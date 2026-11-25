"""Prepare one isolated synthetic draft per approved, committed inbox upload."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import re
from typing import Literal
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from .agent import Authorization, PreparationBlocked, StagePolicy
from .batches import InboxAnalysis, InboxPractice
from .contracts import Amount, Candidate, ExactClaim, Form, LineEvidence, OriginalRef, PageEvidence, RecordIdentity, RegionEvidence
from .coordinator_db import CoordinatorDB, migrate
from .evidence import GroundedCandidate, OriginalScope
from .native_plan import DraftIdentity
from .ownership import OwnerGrant, OwnershipBlocked, OwnershipStore
from .practice_destination import PracticeDestination, PracticeField, PracticeForm
from .providers import Provider, ProviderError
from .session_lock import SessionBusy, hold_session
from .tax_store import FactInput, OriginalPointer, PostgresTaxStore, RevisionView, TaxAccess, TaxRecord
from .worker import NativeSession, OpenProvider, PreparationWorker, WorkerEnrollment, WorkerNative, WorkerPlan, WorkItem

_FIRM = "local-public-practice"
_INBOX_FIRM = "local-workspace"
_INBOX_ACTOR = "local-operator"
_MACHINE = "local-practice-seat"
_WORKER = "local-practice-worker"
_CLASSES = frozenset({"synthetic"})
log = logging.getLogger(__name__)

_SOURCE_FORMS: dict[str, Form] = {"W-2": "w2", "1099-K": "1099k", "1099-NEC": "1099nec"}
_BOX = re.compile(r"Box ([0-9]{1,2}[a-z]?) — .+")
_LITERAL_MONEY = re.compile(r"(?<![\w$.,(\-])\$?[^\S\n]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?$")
_SOURCE_ISSUES = {
    "source-needs-review": "Some source boxes still need review.",
    "unmapped-source-fields": "The practice form does not support every box on this original.",
    "source-field-not-grounded": "A source box was left blank because its citation or amount could not be verified for this form.",
    "missing-primary-source-amount": "The form's primary amount was not entered because it lacked a supported source citation.",
}
_ISSUES = {
    "gemini-rate-limited": "Gemini rate-limited preparation (HTTP 429). No draft was verified.",
    "provider-unavailable": "Gemini could not complete preparation. Check the configured key, model, and service availability.",
    "gemini-service-unavailable": "Gemini remained unavailable (HTTP 503) after retrying. No draft was verified.",
    "model-budget-exhausted": "The assignment reached its Gemini request budget before the draft was verified.",
    "frozen-job-policy-not-authorized": "This assignment's approval expired or its Gemini policy changed. No further preparation was performed.",
    "form-not-supported": "This sample does not identify one supported practice form.",
    "no-grounded-practice-fields": "No supported practice box has a verified citation and an amount this form can accept.",
    "field-conflict": "A practice field differs from the source and was left unchanged.",
    "practice-save-before-fields": "Gemini proposed saving before the remaining supported boxes were entered.",
    "reopen-unverified": "The practice worker could not reopen and verify the saved draft.",
}


def _grounded_amounts(analysis: InboxAnalysis, scope: OriginalScope, record: TaxRecord,
                      media_type: str) -> tuple[dict[str, tuple[int, Amount]], tuple[FactInput, ...], tuple[str, ...]]:
    boxes = [(index, fact, label[1]) for index, fact in enumerate(analysis.facts)
             if (label := _BOX.fullmatch(fact.label))]
    counts = Counter(box for _, _, box in boxes)
    grounded, inputs = {}, []
    issues = {"source-needs-review": None} if analysis.status == "needs_review" else {}
    occurrence = f"occurrence-{uuid4().hex}"
    for index, fact, box in boxes:
        raw = _LITERAL_MONEY.search(fact.quote.rstrip())
        if counts[box] != 1 or not raw:
            issues["source-field-not-grounded"] = None
            continue
        try:
            amount = Amount(kind="amount", value=fact.value)
            literal = raw.group().strip()
            if Amount(kind="amount", value="".join(literal.replace("$", "").replace(",", "").split())) != amount:
                raise ValueError("Literal amount differs from the grounded value.")
            if media_type == "application/pdf" and fact.page is not None and fact.rect is not None:
                # A PDF cell can span lines; its verified rectangle carries the citation.
                claim = PageEvidence(precision="page", original=scope.original, page=fact.page)
                evidence = RegionEvidence(precision="region", original=scope.original,
                                          page=fact.page, rect=fact.rect, quote=fact.quote)
            elif media_type == "text/plain" and fact.page is None and fact.rect is None:
                claim = ExactClaim(precision="exact", original=scope.original, page=None,
                                   line_start=fact.line, line_end=fact.line, quote=fact.quote)
                evidence = LineEvidence(precision="lines", original=scope.original,
                                        line_start=fact.line, line_end=fact.line, quote=fact.quote)
            else:
                raise ValueError("Source precision does not match the original.")
            candidate = Candidate(fact_key=f"box-{box}", column="reported", raw_value=literal,
                                  normalized=amount, citations=(claim,))
        except ValueError:
            issues["source-field-not-grounded"] = None
            continue
        grounded[box] = (index, amount)
        inputs.append(FactInput(
            fact_id=f"fact-{uuid4().hex}", record_id=record.record_id, occurrence_id=occurrence,
            provenance="local-public-sample-grounding",
            grounded=GroundedCandidate(scope=scope, identity=record.identity, candidate=candidate, citations=(evidence,)),
        ))
    return grounded, tuple(inputs), tuple(issues)


def _supported_boxes(destination: PracticeDestination, form: PracticeForm, amounts: dict[str, Amount]) -> dict[str, str]:
    mapped = [(box, field) for box in amounts if (field := form.fields_by_box.get(box))]
    counts = Counter(field for _, field in mapped)
    return {field: box for box, field in mapped if counts[field] == 1
            and destination.accepts_amount(format(amounts[box].value, "f"))}


def _authorize(item: WorkItem) -> Authorization | None:
    try:
        return Authorization.from_wire(item.snapshot["practice"]["authorization"])
    except (KeyError, TypeError, ValueError):
        return None


class _LocalPracticeNative:
    """Keep native sessions scoped to the local public-sample assignment."""

    def __init__(self, native: WorkerNative, store_id: str):
        self._native = native
        self._store_id = store_id

    @asynccontextmanager
    async def session(self, grant: OwnerGrant, item: WorkItem) -> AsyncIterator[NativeSession]:
        if (grant.firm_id != _FIRM or grant.machine_id != _MACHINE or grant.actor_id != _WORKER
                or (item.firm_id, item.return_id, item.job_id, item.store_id)
                != (_FIRM, grant.return_id, grant.job_id, self._store_id)
                or item.identity.client_reference != f"sample-{grant.return_id}"):
            raise OwnershipBlocked("practice-checkout-mismatch")
        async with self._native.session(grant, item) as session:
            yield session


class _PracticeGemini:
    """Expose only sanitized reasoning, with a distinct quota-exhaustion status."""

    def __init__(self, provider: Provider):
        self._provider = provider

    @property
    def identity(self):
        return self._provider.identity

    async def propose(self, observation, tools, previous, *, max_output_tokens, max_request_bytes):
        # Finish the available authorized fields before offering a save.
        choices = observation["choices"]
        defer_save = any(choice["name"] in {"set_field", "set_fields"} for choice in choices) and any(
            choice["name"] == "save_draft" for choice in choices
        )
        if defer_save:
            observation = {**observation, "choices": [choice for choice in choices if choice["name"] != "save_draft"]}
            tools = tuple(tool for tool in tools if tool.name != "save_draft")
        try:
            turn = await self._provider.propose(observation, tools, previous, max_output_tokens=max_output_tokens,
                                                max_request_bytes=max_request_bytes)
        except ProviderError as error:
            # ProviderError is sanitized by the adapter; SDK response bodies stay private.
            log.warning("Practice Gemini request failed: %s", error)
            if error.status == 429:
                raise PreparationBlocked("gemini-rate-limited") from None
            raise
        if defer_save and any(call.name == "save_draft" for call in turn.calls):
            raise PreparationBlocked("practice-save-before-fields")
        return turn

    async def extract(self, instructions, text, schema, *, max_output_tokens, max_request_bytes, before_request=None):
        raise PreparationBlocked("practice-reasoning-only")


class LocalPracticeWorker:
    """A single enrolled, local-only synthetic practice seat and its durable queue."""

    def __init__(self, db: CoordinatorDB, destination: PracticeDestination, policy: StagePolicy,
                 open_reasoning: OpenProvider):
        if policy.identity.provider != "gemini" or policy.stage != "reasoning" or policy.document_classes != _CLASSES:
            raise ValueError("The local practice worker requires a synthetic-only Gemini reasoning policy.")
        self.db, self.policy = db, policy
        self._destination = destination
        self._lock_path = destination.lock_path
        migrate(db)
        self._ownership = OwnershipStore(db, native_authority=destination.authority)
        self._tax = PostgresTaxStore(db)

        @asynccontextmanager
        async def reasoning() -> AsyncIterator[Provider]:
            try:
                async with open_reasoning() as provider:
                    yield _PracticeGemini(provider)
            except ProviderError as error:
                log.warning("Practice Gemini client could not start: %s", error)
                raise PreparationBlocked("provider-unavailable") from None

        self._worker = PreparationWorker(
            db, self._ownership, self._tax, firm_id=_FIRM, approved_sums={},
            enrollment=WorkerEnrollment(
                _MACHINE, _WORKER, policy, _LocalPracticeNative(destination.native, destination.store_id),
                self._lock_path, self._plan, _authorize, reasoning,
            ),
        )
        self._ownership.register_machine(_FIRM, _MACHINE, _WORKER)
        self._enroll_seat()

    def _plan(self, item: WorkItem, view: RevisionView) -> WorkerPlan:
        frozen = item.snapshot["practice"]
        if len(view.records) != 1:
            raise PreparationBlocked("practice-source-record-mismatch")
        record, = view.records.values()
        form = self._destination.form(record.identity.form)
        if (record.identity.subject_reference != item.identity.client_reference
                or record.identity.tax_year != item.identity.tax_year
                or form is None or form.section_key != frozen["section"]):
            raise PreparationBlocked("practice-source-record-mismatch")
        sources: dict[str, PracticeField] = {}
        for key, facts in view.effective_facts.items():
            if (key.record_id != record.record_id or not key.fact_key.startswith("box-") or len(facts) != 1
                    or not isinstance(facts[0].normalized, Amount) or not facts[0].eligible):
                raise PreparationBlocked("practice-source-field-mismatch")
            fact, = facts
            sources[key.fact_key.removeprefix("box-")] = PracticeField(key, fact.fact_id, fact.normalized)
        selected = _supported_boxes(self._destination, form, {box: fact.amount for box, fact in sources.items()})
        fields = {field: sources[box] for field, box in selected.items()}
        if fields.keys() != frozen["fields"].keys() or not fields:
            raise PreparationBlocked("practice-source-field-mismatch")
        return self._destination.plan(item.identity, record, fields, tuple(frozen["source_issues"]))

    def _enroll_seat(self) -> None:
        try:
            with hold_session(self._lock_path):
                with self.db.transaction() as conn:
                    owner = conn.execute("""
                        SELECT * FROM prep_owners WHERE firm_id=%s AND machine_id=%s AND released_at IS NULL
                    """, (_FIRM, _MACHINE)).fetchone()
                    machine = conn.execute("""
                        SELECT cordoned FROM prep_machines WHERE firm_id=%s AND machine_id=%s
                    """, (_FIRM, _MACHINE)).fetchone()
                if owner is not None and not owner["reconciling"]:
                    # The local lock becoming free is not proof that a prior native write
                    # stopped. Keep the journal and checkout blocked for reconciliation.
                    self._ownership.interrupt(OwnerGrant(
                        _FIRM, owner["return_id"], owner["generation"], _MACHINE,
                        owner["actor_id"], owner["job_id"], owner["review_id"],
                    ), "uncertain")
                elif owner is None and not machine["cordoned"]:
                    self._ownership.set_available(_FIRM, _MACHINE, True)
        except SessionBusy:
            return

    def enqueue(self, conn: Connection[DictRow], *, upload_id: str, version_id: str, digest: str,
                analysis: InboxAnalysis, tax_year: int, approval_expires_at: float) -> None:
        """Freeze a committed public sample and its caller-verified explicit grant together."""
        original = OriginalRef(document_id=upload_id, version_id=version_id)
        expires = datetime.fromtimestamp(approval_expires_at, timezone.utc)
        slot = conn.execute("""
            SELECT s.object_id,s.media_type FROM prep_upload_slots s
            JOIN prep_upload_outbox o USING (firm_id,upload_id,version_id)
            WHERE (s.firm_id,s.actor_id,s.upload_id,s.version_id,s.digest)=(%s,%s,%s,%s,%s)
            FOR UPDATE OF s
        """, (_INBOX_FIRM, _INBOX_ACTOR, upload_id, version_id, digest)).fetchone()
        if slot is None:
            raise ValueError("Practice preparation requires the committed immutable inbox upload.")
        prior = conn.execute("SELECT 1 FROM prep_jobs WHERE firm_id=%s AND job_id=%s", (_FIRM, upload_id)).fetchone()
        if prior is not None:
            return  # Redelivery and repeated approval never generate another native draft.
        return_id, revision_id = uuid4().hex, uuid4().hex
        identity = DraftIdentity(client_reference=f"sample-{return_id}", tax_year=tax_year, return_type="individual")
        scope = OriginalScope(firm_id=_FIRM, return_id=return_id, original=original)
        pointer = OriginalPointer(original=original, storage_ref=slot["object_id"], digest=digest, media_type=slot["media_type"])
        form_type = _SOURCE_FORMS.get(analysis.forms[0]) if len(analysis.forms) == 1 else None
        form = self._destination.form(form_type) if form_type is not None else None
        record = None
        fields, facts, source_issues = {}, (), ()
        section = form.section_key if form is not None else None
        if form_type is not None and analysis.status in {"filed", "needs_review"}:
            record_id = f"record-{uuid4().hex}"
            record = TaxRecord(record_id=record_id, identity=RecordIdentity(
                tax_year=tax_year, form=form_type, subject_reference=identity.client_reference,
                issuer_reference=f"issuer-{uuid4().hex}", instance_reference="ORIGINAL",
                activity_reference=None, period=None,
            ))
            grounded, facts, source_issues = _grounded_amounts(analysis, scope, record, pointer.media_type)
            if form is not None:
                selected = _supported_boxes(self._destination, form, {box: value for box, (_, value) in grounded.items()})
                fields = {field: grounded[box][0] for field, box in selected.items()}
                if any(box not in form.fields_by_box for box in grounded):
                    source_issues += ("unmapped-source-fields",)
                if len(fields) < sum(box in form.fields_by_box for box in grounded):
                    source_issues = tuple(dict.fromkeys((*source_issues, "source-field-not-grounded")))
                if form.primary_field not in fields:
                    source_issues += ("missing-primary-source-amount",)
        issue = None if fields else ("form-not-supported" if form is None else "no-grounded-practice-fields")
        access = TaxAccess(firm_id=_FIRM, return_id=return_id, actor_id=_INBOX_ACTOR, scopes={"ingest"})
        with self._tax.using_connection(conn) as store:
            store.create_return(access, identity.tax_year)
            revision = store.append_revision(access, 0, originals=(pointer,), records=(record,) if facts else (), facts=facts)
        authorization = Authorization(revision_id, "reasoning", self.policy.identity, _CLASSES, expires)
        snapshot = {
            "parent_revision": None,
            "reasoning_policy": self.policy.to_wire(),
            "inputs": [{"artifact_id": str(revision), "upload_id": upload_id, "version_id": version_id,
                        "object_id": pointer.storage_ref, "sha256": digest, "extraction_policy": {"data_class": "synthetic"}}],
            "practice": {"section": section, "fields": fields, "source_issues": source_issues,
                         "issue": issue, "authorization": authorization.to_wire()},
        }
        conn.execute("""
            INSERT INTO prep_returns (firm_id,return_id,profile_id,store_id,tax_year,review_required)
            VALUES (%s,%s,%s,%s,%s,true)
        """, (_FIRM, return_id, self._destination.profile_id, self._destination.store_id, tax_year))
        conn.execute("""
            INSERT INTO prep_return_directory
                (firm_id,return_id,client_id,client_name,client_reference,return_type,product)
            VALUES (%s,%s,%s,'Public practice sample',%s,'individual',%s)
        """, (_FIRM, return_id, return_id, identity.client_reference, self._destination.product))
        conn.execute("""
            INSERT INTO prep_jobs (firm_id,job_id,return_id,input_revision,profile_id,revision_sequence,state,snapshot)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (_FIRM, upload_id, return_id, revision_id, self._destination.profile_id, revision,
              "needs_attention" if issue else "queued", Jsonb(snapshot)))
        if issue is None:
            # Only the year supplied by the checked fingerprint qualifies this local seat.
            conn.execute("""
                INSERT INTO prep_machine_profiles (firm_id,machine_id,profile_id,tax_year,store_id,shared_store,checkout_mode)
                VALUES (%s,%s,%s,%s,%s,false,'firm') ON CONFLICT DO NOTHING
            """, (_FIRM, _MACHINE, self._destination.profile_id, tax_year, self._destination.store_id))
            conn.execute("INSERT INTO prep_job_outbox (firm_id,job_id) VALUES (%s,%s)", (_FIRM, upload_id))

    async def run_once(self) -> str | None:
        return await self._worker.run_once()

    def status(self, upload_id: str) -> dict | None:
        """Project only released, independently reopened fields into the inbox."""
        with self.db.transaction() as conn:
            row = conn.execute("""
                SELECT j.state, j.snapshot->'practice' AS practice, t.draft_key, m.cordoned,
                       verified.result, issue.event
                FROM prep_jobs j
                JOIN prep_returns t USING (firm_id,return_id)
                JOIN prep_machines m ON (m.firm_id,m.machine_id)=(j.firm_id,%s)
                LEFT JOIN LATERAL (
                    SELECT v.result FROM prep_verifications v JOIN prep_owners o USING (firm_id,return_id,generation)
                    WHERE (v.firm_id,v.job_id)=(j.firm_id,j.job_id) AND o.released_at IS NOT NULL
                    ORDER BY v.generation DESC LIMIT 1
                ) verified ON true
                LEFT JOIN LATERAL (
                    SELECT e.event FROM prep_owner_events e
                    WHERE (e.firm_id,e.return_id)=(j.firm_id,j.return_id) AND e.event LIKE 'issue:%%'
                    ORDER BY e.event_order DESC LIMIT 1
                ) issue ON true
                WHERE (j.firm_id,j.job_id,j.profile_id)=(%s,%s,%s)
            """, (_MACHINE, _FIRM, upload_id, self._destination.profile_id)).fetchone()
        if row is None:
            return None
        state = row["state"]
        if state == "queued":
            result = (InboxPractice(status="waiting", gated="Practice preparation is paused until an interrupted draft is reconciled.")
                      if row["cordoned"] else InboxPractice(status="queued"))
        elif state in {"preparing", "verifying"}:
            result = InboxPractice(status="preparing", message="Reopening the saved practice draft to verify it."
                                   if state == "verifying" else None)
        elif state == "reconciling":
            result = InboxPractice(status="needs_review", message=(
                "A native write was interrupted. The practice draft must be reconciled before this seat can resume."
            ))
        else:
            result = self._finished_status(row)
        return result.model_dump()

    def _finished_status(self, row: DictRow) -> InboxPractice:
        frozen = row["practice"]
        verification = row["result"] or {}
        issues = verification.get("issues", ())
        code = row["event"].removeprefix("issue:") if row["event"] else frozen["issue"]
        section, draft = frozen["section"], row["draft_key"]
        verified = set(verification.get("verified_binding_ids", ()))
        readings = tuple(
            reading for reading in verification.get("readbacks", ())
            if reading["binding_id"] in verified and reading["section_key"] == section
            and reading["field_key"] in frozen["fields"]
        )
        bindings = {reading["field_key"]: frozen["fields"][reading["field_key"]] for reading in readings}
        if (not code and draft is not None and section is not None
                and verification.get("scope", {}).get("draft_key") == draft
                and bindings.keys() == frozen["fields"].keys() and bindings and set(issues) <= _SOURCE_ISSUES.keys()):
            row_keys = {reading["native_row_key"] for reading in readings}
            url = self._destination.draft_url(draft, section, row_keys)
            if url is not None:
                status: Literal["ready", "needs_review"] = "needs_review" if issues else "ready"
                message = " ".join(_SOURCE_ISSUES[key] for key in issues) if issues else None
                return InboxPractice(status=status, url=url, bindings=bindings, message=message)
        issue = code or next(iter(issues), "reopen-unverified")
        message = _ISSUES.get(issue, "The practice draft could not be confirmed. Review the original; no unverified draft link is available.")
        return InboxPractice(status="failed" if issue in {
            "gemini-rate-limited", "gemini-service-unavailable", "provider-unavailable", "worker-failed",
        }
                             else "needs_review", message=message)
