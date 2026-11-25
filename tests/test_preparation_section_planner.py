"""Verify offline section selection against the same contract enforced by the worker."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.preparation.contracts import (
    Amount, DocumentEvidence, ExtractionIssue, LineEvidence, OriginalRef, RecordIdentity, TextValue,
)
from src.preparation.native_plan import DraftIdentity
from src.preparation.section_planner import plan_individual
from src.preparation.tax_store import (
    CitedFact, FieldState, FieldUpdate, NativeBinding, NativeDestination, NativeReading, OriginalPointer,
    RevisionView, SourceField, TaxRecord, effective_source_facts,
)
from src.preparation.worker import WorkItem, _validate_plan, binding_reference


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PERSON = DraftIdentity(client_reference="synthetic-subject", tax_year=2025, return_type="individual")
ITEM = WorkItem("firm-a", "job-a", "return-a", "opaque-revision", "synthetic-draft-v1", "practice-store",
                None, PERSON, {})


def document(record_id, form, fields, *, issuer="Example Ride", activity="RIDESHARE-01", period="2025",
             instance=None, subject=PERSON.client_reference, year=2025, verified=True, labels=None):
    original = OriginalRef(document_id=record_id, version_id="version-a")
    record = TaxRecord(record_id=record_id, identity=RecordIdentity(
        tax_year=year, form=form, subject_reference=subject, issuer_reference=issuer,
        activity_reference=activity, period=period, instance_reference=instance or record_id,
    ))
    pointer = OriginalPointer(original=original, storage_ref=record_id, digest="a" * 64, media_type="text/plain")
    facts = []
    for line, (key, value) in enumerate(fields.items(), 1):
        raw = f"{(labels or {}).get(key, key)}: {value}"
        citation = (LineEvidence(precision="lines", original=original, line_start=line, line_end=line, quote=raw)
                    if verified else DocumentEvidence(precision="document", original=original))
        facts.append(CitedFact(
            fact_id=f"{record_id}-{key}", record_id=record_id, occurrence_id=f"occurrence-{record_id}",
            original=original, fact_key=key, column="reported", raw_value=raw, provenance="synthetic-test",
            normalized=TextValue(kind="text", value=value) if key == "description" else Amount(kind="amount", value=value),
            citations=(citation,),
        ))
    return record, pointer, facts


def wages(record_id="w2-a", *, employer="employer-a", **kwargs):
    return document(record_id, "w2", {"box-1": "7200.10", "box-2": "0"}, issuer=employer,
                    activity=None, period=None, instance="original", **kwargs)


def summary(*, overlap=True, **kwargs):
    labels = {"gross-receipts": "Gross fares (also reported on the 1099-K)"} if overlap else None
    return document("summary", "platform_summary", {"gross-receipts": "6200.00", "platform-fees": "600.00"},
                    labels=labels, **kwargs)


def form_k(amount="6200", **kwargs):
    return document("1099-k", "1099k", {"gross-receipts": amount}, **kwargs)


def receipt(record_id="receipt-a", **kwargs):
    return document(record_id, "receipt", {"description": "Safety supplies", "amount": "34.50"},
                    issuer="merchant-a", **kwargs)


def revision(*documents, issues=(), fields=None, destination=None):
    records = {record.record_id: record for record, _, _ in documents}
    facts = {fact.fact_id: fact for _, _, reported in documents for fact in reported}
    return RevisionView(
        revision=1, field_version=1, decision_version=0, created_at=NOW, created_by="ingest-service",
        review_required_new_document=False, destination=destination,
        originals=tuple(pointer for _, pointer, _ in documents), issues=issues, records=records, facts=facts,
        fields=fields or {}, changes=(), decisions=(),
        effective_facts=effective_source_facts([(1, f) for f in facts.values()]),
        fact_revisions=dict.fromkeys(facts, 1),
    )


def statuses(selection):
    return {decision.section: decision.status for decision in selection.decisions}


def sections(selection, view, item=ITEM):
    assert selection.plan is not None
    assert selection.issues == selection.plan.review_issues
    assert _validate_plan(item, view, selection.plan, {}).keys() == selection.plan.sources.keys()
    return {section.section_key: section for section in selection.plan.draft.sections}


def test_w2_only_selects_distinct_employers_and_leaves_other_sections_untouched():
    view = revision(wages("w2-b", employer="employer-b"), wages())
    selection = plan_individual(ITEM, view)
    assert statuses(selection) == {"w2": "selected", "rideshare": "skipped", "expenses": "skipped"}
    assert not selection.issues
    approved = sections(selection, view)
    assert approved.keys() == {"w2"}
    rows = approved["w2"].records
    assert [(row.source_record_id, row.identity, row.allow_create) for row in rows] == [
        ("w2-a", {"employer": "employer-a", "instance": "original"}, True),
        ("w2-b", {"employer": "employer-b", "instance": "original"}, True),
    ]
    assert {f.field_key: (f.value, f.fact_ids) for f in rows[0].fields} == {
        "wages": ("7200.10", ("w2-a-box-1",)), "withholding": ("0", ("w2-a-box-2",)),
    }


def test_mixed_documents_deduplicate_known_gross_and_leave_nec_for_review():
    nec = document("incentive", "1099nec", {"gross-receipts": "150.00"})
    inputs = (receipt("receipt-b"), summary(), wages(), form_k(), receipt(), nec)
    view = revision(*inputs)
    selection = plan_individual(ITEM, view)
    assert selection == plan_individual(ITEM, revision(*reversed(inputs)))
    assert statuses(selection) == {"w2": "selected", "rideshare": "selected", "expenses": "selected",
                                   "documents": "needs_review"}
    assert selection.issues == ("unresolved-1099-nec-overlap",)
    approved = sections(selection, view)
    gross, fees = approved["rideshare"].fields
    assert (gross.field_key, gross.value, gross.fact_ids) == ("gross", "6200", ("1099-k-gross-receipts",))
    assert (fees.field_key, fees.value, fees.fact_ids) == ("fees", "600.00", ("summary-platform-fees",))
    assert view.facts["summary-gross-receipts"].eligible
    assert [(r.identity, [(f.field_key, f.value) for f in r.fields]) for r in approved["expenses"].records] == [
        ({"receipt": "receipt-a"}, [("description", "Safety supplies"), ("expense", "34.50")]),
        ({"receipt": "receipt-b"}, [("description", "Safety supplies"), ("expense", "34.50")]),
    ]


@pytest.mark.parametrize(("report", "issue"), [
    (form_k("6300"), "conflicting-rideshare-gross"),
    (form_k(activity="RIDESHARE-02"), "ambiguous-rideshare-scope"),
    (form_k(issuer="platform-b"), "ambiguous-rideshare-scope"),
    (form_k(period="different-period"), "ambiguous-rideshare-scope"),
    (form_k(verified=False), "unverified-rideshare-gross"),
])
def test_unreconciled_reports_block_rideshare_and_its_expenses_but_not_w2(report, issue):
    view = revision(summary(), report, receipt(), wages())
    selection = plan_individual(ITEM, view)
    assert statuses(selection) == {"w2": "selected", "rideshare": "needs_review", "expenses": "needs_review"}
    assert issue in selection.issues
    assert sections(selection, view).keys() == {"w2"}


def test_equal_gross_without_evidence_of_overlap_does_not_authorize_rideshare():
    view = revision(summary(overlap=False), form_k())
    selection = plan_individual(ITEM, view)
    assert selection.plan is None
    assert statuses(selection)["rideshare"] == "needs_review"
    assert selection.issues == ("unverified-rideshare-overlap",)


def test_duplicate_receipt_identity_is_not_a_license_to_create_two_expenses():
    view = revision(summary(), receipt("receipt-a", instance="same-receipt"),
                    receipt("receipt-b", instance="same-receipt"), receipt("receipt-c", period="another-period"))
    selection = plan_individual(ITEM, view)
    assert statuses(selection)["expenses"] == "needs_review"
    assert set(selection.issues) == {"ambiguous-native-row", "unverified-receipt-identity"}
    assert sections(selection, view).keys() == {"rideshare"}


def test_unknown_and_unsupported_documents_do_not_select_any_section():
    unknown = OriginalRef(document_id="unknown", version_id="version-a")
    business = document("corporate-ledger", "business_ledger", {"gross-receipts": "12000"})
    view = revision(business, issues=(ExtractionIssue(original=unknown, code="unknown_form"),))
    selection = plan_individual(ITEM, view)
    assert selection.plan is None
    assert statuses(selection) == {"w2": "skipped", "rideshare": "skipped", "expenses": "skipped",
                                   "documents": "needs_review"}
    assert set(selection.issues) == {"unsupported-source-form", "source-unknown-form"}


@pytest.mark.parametrize("report", [form_k(activity="RETAIL-01"), form_k(issuer="Unapproved Platform")])
def test_a_1099k_without_approved_rideshare_identity_cannot_select_schedule_c(report):
    selection = plan_individual(ITEM, revision(report))
    assert selection.plan is None
    assert statuses(selection)["rideshare"] == "needs_review"
    assert selection.issues == ("unsupported-rideshare-activity",)


def test_receipt_alone_cannot_route_to_rideshare_and_no_destination_is_guessed():
    selection = plan_individual(ITEM, revision(receipt()))
    assert selection.plan is None
    assert statuses(selection) == {"w2": "skipped", "rideshare": "skipped", "expenses": "needs_review"}
    view = revision(summary(), receipt())
    selection = plan_individual(ITEM, view)
    assert sections(selection, view).keys() == {"rideshare", "expenses"}
    assert selection.plan.transit_contexts == {"w2"}


def test_cross_subject_and_year_facts_with_unverified_boxes_cannot_write():
    view = revision(wages("other-client", subject="another-subject"), wages("other-year", year=2024),
                    wages(verified=False))
    selection = plan_individual(ITEM, view)
    assert selection.plan is None
    assert "source-return-mismatch" in selection.issues
    assert statuses(selection)["w2"] == "needs_review"


@pytest.mark.parametrize("identity", [PERSON.model_copy(update={"return_type": "1120"}),
                                     PERSON.model_copy(update={"tax_year": 2024})])
def test_unsupported_type_and_year_do_not_get_the_individual_mapping(identity):
    result = plan_individual(replace(ITEM, identity=identity), revision(wages()))
    assert result.plan is None
    assert result.issues == ("unsupported-individual-profile",)


def test_existing_row_baseline_and_preparer_lock_are_reused_without_creating_another_row():
    bid = binding_reference("w2", "w2-a", "wages")
    original = Amount(kind="amount", value="7000.10")
    field = FieldUpdate(
        binding=NativeBinding(binding_id=bid, section="w2", record_key="existing-row", field_key="wages",
                              source_fields=(SourceField(record_id="w2-a", fact_key="box-1"),)),
        state=FieldState(reviewer_saved=NativeReading(value=original, observed_at=NOW)),
    )
    view = revision(wages(), fields={bid: field},
                    destination=NativeDestination(profile_id=ITEM.profile_id, draft_key="native-draft"))
    item = replace(ITEM, draft_key="native-draft")
    selection = plan_individual(item, view)
    row, = sections(selection, view, item)["w2"].records
    assert row.external_key == "existing-row" and not row.allow_create
    assert row.fields[0].expected_saved == "7000.10" and row.fields[0].preparer_owned
    assert not row.fields[0].may_enter("7000.10")
    assert selection.issues == ("preparer-owned-value",)
    assert plan_individual(ITEM, view).plan is None
