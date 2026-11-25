"""Select sections for the synthetic individual web profile from scoped evidence."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
from typing import Literal

from .contracts import Amount, LineEvidence, MissingValue, RegionEvidence, TextValue
from .native_plan import DraftPlan, FieldPlan, RecordPlan, SectionPlan
from .tax_store import CitedFact, NativeBinding, RevisionView, SourceField, TaxRecord
from .worker import FieldSource, WorkerPlan, WorkItem, binding_reference


_PROFILE = "synthetic-draft-v1"
_YEAR = 2025
_RIDESHARE_ISSUER = "Example Ride"
_RIDESHARE_ACTIVITY = re.compile(r"RIDESHARE-[0-9]{2}\Z")
_OVERLAP_LABEL = "Gross fares (also reported on the 1099-K): "
_NATIVE_ROW = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
Section = Literal["w2", "rideshare", "expenses", "documents"]
Status = Literal["selected", "skipped", "needs_review"]


@dataclass(frozen=True)
class SectionDecision:
    section: Section
    status: Status
    reason: str
    issue_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectionSelection:
    plan: WorkerPlan | None
    decisions: tuple[SectionDecision, ...]

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(code for decision in self.decisions for code in decision.issue_codes))


class _Selection:
    def __init__(self, view: RevisionView):
        self.view = view
        self.originals = {pointer.original for pointer in view.originals}
        self.reported: dict[SourceField, list[CitedFact]] = defaultdict(list)
        for fact in view.facts.values():
            self.reported[SourceField(record_id=fact.record_id, fact_key=fact.fact_key)].append(fact)
        self.notes: dict[Section, dict[str, str]] = defaultdict(dict)
        self.sources: dict[str, FieldSource] = {}

    def issue(self, section: Section, code: str, reason: str) -> None:
        self.notes[section][code] = reason

    def facts(self, section: Section, record: TaxRecord, key: str, *, text: bool = False) -> tuple[CitedFact, ...]:
        source = SourceField(record_id=record.record_id, fact_key=key)
        facts = self.view.effective_facts.get(source, ())
        if not facts and all(isinstance(f.normalized, MissingValue) for f in self.reported[source]):
            return ()
        if not facts or any(
            fact.record_id != source.record_id or fact.fact_key != source.fact_key
            or self.view.facts.get(fact.fact_id) != fact or fact.original not in self.originals
            or any(citation.original != fact.original for citation in fact.citations) or not fact.eligible
            or (not isinstance(fact.normalized, TextValue) or not fact.normalized.value.strip()
                if text else not isinstance(fact.normalized, Amount))
            or fact.normalized != facts[0].normalized
            for fact in facts
        ):
            self.issue(section, f"unverified-{section}-{key}",
                       f"Verify the source for {key}, including any conflicting values or missing citation.")
            return ()
        return tuple(sorted(facts, key=lambda fact: fact.fact_id))

    def field(self, section: Section, row_id: str | None, row_key: str | None, field_key: str,
              facts: tuple[CitedFact, ...]) -> FieldPlan | None:
        if not facts:
            return None
        binding_id = binding_reference(section, row_id, field_key)
        value = facts[0].normalized
        assert isinstance(value, (Amount, TextValue))
        source = SourceField(record_id=facts[0].record_id, fact_key=facts[0].fact_key)
        old = self.view.fields.get(binding_id)
        baseline = None
        locked = False
        if old:
            expected = NativeBinding(binding_id=binding_id, section=section, record_key=row_key,
                                     field_key=field_key, source_fields=(source,))
            saved = old.state.last_saved
            if (old.binding != expected or (saved is not None and not isinstance(saved.value, type(value))
                                           and saved.value.kind != "missing")):
                self.issue(section, "native-binding-conflict", "Reconcile the existing native field and its source binding.")
                return None
            locked = old.state.reviewer_saved is not None
            if saved is not None:
                baseline = _render(saved.value) if isinstance(saved.value, (Amount, TextValue)) else ""
            if locked and saved is not None and saved.value != value:
                self.issue(section, "preparer-owned-value", "Compare the proposed value with the preparer's saved entry.")
        self.sources[binding_id] = FieldSource((source,), value)
        return FieldPlan(field_key=field_key, value=_render(value), binding_id=binding_id,
                         fact_ids=tuple(fact.fact_id for fact in facts[:32]), expected_saved=baseline,
                         preparer_owned=locked, kind="decimal" if isinstance(value, Amount) else "text")

    def rows(self, section: Section, candidates: list[tuple[TaxRecord, dict[str, str], dict[str, tuple[CitedFact, ...]]]],
             native_fields: tuple[str, ...]) -> tuple[RecordPlan, ...]:
        identities = Counter(tuple(identity.items()) for _, identity, _ in candidates)
        resolved = []
        for record, identity, source_fields in candidates:
            row_id = record.record_id
            existing = [self.view.fields[binding_id].binding.record_key for key in native_fields
                        if (binding_id := binding_reference(section, row_id, key)) in self.view.fields]
            keys = set(existing)
            if (len(keys) > 1 or any(key is None or not _NATIVE_ROW.fullmatch(key) or key == "new" for key in keys)):
                self.issue(section, "native-row-conflict", "Reconcile the existing native row identity.")
                continue
            resolved.append((row_id, identity, source_fields, next(iter(keys), None)))
        row_keys = Counter(key for _, _, _, key in resolved if key is not None)
        output = []
        for row_id, identity, source_fields, row_key in resolved:
            if identities[tuple(identity.items())] != 1 or (row_key is not None and row_keys[row_key] != 1):
                self.issue(section, "ambiguous-native-row", "Identify the unique destination row before entering these records.")
                continue
            fields = tuple(field for key, facts in source_fields.items()
                           if (field := self.field(section, row_id, row_key, key, facts)) is not None)
            if section == "expenses" and len(fields) != len(native_fields):
                for field in fields:
                    del self.sources[field.binding_id]
                continue
            if fields:
                correction = any(f.column == "correct_information" for facts in source_fields.values() for f in facts)
                output.append(RecordPlan(source_record_id=row_id, identity=identity, fields=fields,
                                         external_key=row_key, allow_create=row_key is None and not correction))
        return tuple(output)

    def decision(self, section: Section, selected: bool, reason: str) -> SectionDecision:
        notes = self.notes[section]
        return SectionDecision(section, "selected" if selected else "needs_review" if notes else "skipped",
                               " ".join((reason, *notes.values())), tuple(notes))


def _render(value: Amount | TextValue) -> str:
    return format(value.value, "f") if isinstance(value, Amount) else value.value


def _present(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _wages(selection: _Selection, records: list[TaxRecord], *, require_box_3: bool) -> tuple[RecordPlan, ...]:
    rows = []
    for record in records:
        identity = record.identity
        issuer = identity.issuer_reference
        if issuer is None or not issuer.strip() or not _present(identity.instance_reference):
            selection.issue("w2", "unverified-w2-identity", "Verify the W-2 employer and form instance.")
            continue
        fields = {
            "wages": selection.facts("w2", record, "box-1"),
            "withholding": selection.facts("w2", record, "box-2"),
        }
        if require_box_3:
            fields["social_security_wages"] = selection.facts("w2", record, "social_security_wages")
            record_label = f"{issuer}/{identity.instance_reference}"
            issue_key = re.sub(r"[^a-z0-9-]", "-", record_label.lower())[:76]
            for box, (field_key, facts) in enumerate(fields.items(), start=1):
                if not facts:
                    selection.issue("w2", f"missing-w2-box-{box}-{issue_key}",
                                    f"Review W-2 Box {box} for {record_label}; {field_key} has no verified value.")
        rows.append((record, {"employer": issuer, "instance": identity.instance_reference}, fields))
    return selection.rows("w2", rows, ("wages", "withholding", "social_security_wages"))


def _rideshare(selection: _Selection, summaries: list[TaxRecord], reports: list[TaxRecord]) -> tuple[
    tuple[FieldPlan, ...], tuple[str, str] | None
]:
    records = summaries + reports
    if not records:
        return (), None
    scopes = {(r.identity.issuer_reference, r.identity.activity_reference, r.identity.period) for r in records}
    if len(scopes) != 1 or len(summaries) > 1 or len(reports) > 1 or not all(map(_present, next(iter(scopes)))):
        selection.issue("rideshare", "ambiguous-rideshare-scope",
                        "Reconcile the issuer, activity, period and form instances before using this single rideshare section.")
        return (), None
    issuer, activity, period = next(iter(scopes))
    if (issuer != _RIDESHARE_ISSUER or activity is None or not _RIDESHARE_ACTIVITY.fullmatch(activity)
        or period != str(_YEAR) or any(not _present(r.identity.instance_reference) for r in records)):
        selection.issue("rideshare", "unsupported-rideshare-activity",
                        "Confirm an approved practice platform, rideshare activity and annual reporting period.")
        return (), None
    gross = {r.record_id: selection.facts("rideshare", r, "gross-receipts") for r in records}
    if not all(gross.values()):
        selection.issue("rideshare", "unverified-rideshare-gross", "Verify the gross receipts on each supplied report.")
        return (), None
    if len({facts[0].normalized for facts in gross.values()}) != 1:
        selection.issue("rideshare", "conflicting-rideshare-gross", "Reconcile the conflicting gross reports; do not add them.")
        return (), None
    if summaries and reports and not any(
        isinstance(citation, (LineEvidence, RegionEvidence)) and citation.quote.startswith(_OVERLAP_LABEL)
        for fact in gross[summaries[0].record_id] for citation in fact.citations
    ):
        selection.issue("rideshare", "unverified-rideshare-overlap",
                        "Confirm that the matching amounts are the same receipts, not separate payments.")
        return (), None
    # The worker direct mapping takes one logical source field, not an arithmetic rule.
    # Both originals remain in the revision. Prefer the prior bound source if it still corroborates.
    prior = selection.view.fields.get(binding_reference("rideshare", None, "gross"))
    base = (reports or summaries)[0].record_id
    if prior and len(prior.binding.source_fields) == 1:
        previous, = prior.binding.source_fields
        if previous.fact_key == "gross-receipts" and previous.record_id in gross:
            base = previous.record_id
    fields = [selection.field("rideshare", None, None, "gross", gross[base])]
    if fields[0] is None:
        return (), None
    if summaries:
        fees = selection.facts("rideshare", summaries[0], "platform-fees")
        fields.append(selection.field("rideshare", None, None, "fees", fees))
    return tuple(field for field in fields if field is not None), (activity, period)


def _expenses(selection: _Selection, records: list[TaxRecord], scope: tuple[str, str] | None) -> tuple[RecordPlan, ...]:
    if scope is None and records:
        selection.issue("expenses", "unverified-expense-activity",
                        "Confirm supported rideshare gross receipts and the activity before using nested Expenses.")
        return ()
    rows = []
    for record in records:
        identity = record.identity
        if (identity.activity_reference, identity.period) != scope or not all(map(
            _present, (identity.issuer_reference, identity.instance_reference)
        )):
            selection.issue("expenses", "unverified-receipt-identity", "Verify the receipt issuer, instance, activity and period.")
            continue
        description = selection.facts("expenses", record, "description", text=True)
        amount = selection.facts("expenses", record, "amount")
        shared = {(f.occurrence_id, f.original) for f in description} & {(f.occurrence_id, f.original) for f in amount}
        if not shared:
            selection.issue("expenses", "incomplete-expense-receipt", "Verify a description and amount together on the same receipt.")
        rows.append((record, {"receipt": identity.instance_reference}, {
            "description": tuple(f for f in description if (f.occurrence_id, f.original) in shared),
            "expense": tuple(f for f in amount if (f.occurrence_id, f.original) in shared),
        }))
    return selection.rows("expenses", rows, ("description", "expense"))


def plan_individual(item: WorkItem, view: RevisionView, *, require_box_3: bool = False) -> SectionSelection:
    """Map verified records to the enrolled synthetic profile; never select a filing type or destination."""
    selection = _Selection(view)
    if item.profile_id != _PROFILE or item.identity.return_type != "individual" or item.identity.tax_year != _YEAR:
        selection.issue("documents", "unsupported-individual-profile", "Use an approved connector profile for this return type and year.")
        return SectionSelection(None, (selection.decision("documents", False, "No approved section mapping."),))
    if (view.destination is not None and (view.destination.profile_id, view.destination.draft_key)
        != (item.profile_id, item.draft_key)) or (view.fields and view.destination is None):
        selection.issue("documents", "native-destination-conflict", "Reconcile the saved destination with the enrolled draft.")
        return SectionSelection(None, (selection.decision("documents", False, "No writes to an unverified draft."),))

    by_form = defaultdict(list)
    for record in sorted(view.records.values(), key=lambda row: row.record_id):
        ident = record.identity
        if (ident.subject_reference, ident.tax_year) != (item.identity.client_reference, item.identity.tax_year):
            selection.issue("documents", "source-return-mismatch", "Reassign the document to its exact subject and tax year.")
        elif ident.form == "1099nec":
            selection.issue("documents", "unresolved-1099-nec-overlap", "Determine whether the 1099-NEC overlaps the platform gross before entering it.")
        elif ident.form not in {"w2", "platform_summary", "1099k", "receipt"}:
            selection.issue("documents", "unsupported-source-form", "Review the document manually; its form has no approved mapping.")
        else:
            by_form[ident.form].append(record)
    for issue in view.issues:
        kind = issue.code.replace("_", "-")
        selection.issue("documents", f"source-{kind}", f"Review the source document marked {kind.replace('-', ' ')}.")

    wages = _wages(selection, by_form["w2"], require_box_3=require_box_3)
    rideshare, scope = _rideshare(selection, by_form["platform_summary"], by_form["1099k"])
    expenses = _expenses(selection, by_form["receipt"], scope)
    decisions = (
        selection.decision("w2", bool(wages), "Verified W-2 boxes." if wages else "No supported W-2 entry."),
        selection.decision("rideshare", bool(rideshare), "Verified rideshare reports." if rideshare else "No supported rideshare entry."),
        selection.decision("expenses", bool(expenses), "Verified receipts for this activity." if expenses else "No supported expense entry."),
    )
    if selection.notes["documents"]:
        decisions += (selection.decision("documents", False, "Some documents need preparer review."),)
    result = SectionSelection(None, decisions)
    sections = []
    if wages:
        sections.append(SectionPlan(section_key="w2", records=wages))
    if rideshare:
        sections.append(SectionPlan(section_key="rideshare", fields=rideshare))
    if expenses:
        sections.append(SectionPlan(section_key="expenses", records=expenses))
    if not sections:
        return result
    return SectionSelection(WorkerPlan(
        draft=DraftPlan(identity=item.identity, sections=tuple(sections)), sources=selection.sources,
        transit_contexts=frozenset({"w2"}) if not wages else frozenset(), review_issues=result.issues,
    ), decisions)
