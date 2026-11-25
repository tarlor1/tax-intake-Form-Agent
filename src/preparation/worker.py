"""Run one assigned return on an enrolled interactive seat with a durable journal."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Protocol

from .agent import Authorization, DraftAssignment, PreparationBlocked, StagePolicy, prepare
from .contracts import Amount, BooleanValue, FactValue, MissingValue, TextValue
from .coordinator_db import CoordinatorDB
from .native_plan import DraftIdentity, DraftPlan, FieldPlan, RecordPlan
from .ownership import OwnerGrant, OwnershipBlocked, OwnershipJournal, OwnershipStore
from .providers import Provider
from .session_lock import hold_session, SessionBusy
from .tax_store import (
    ApprovedSum, DerivationAddress, FieldState, FieldUpdate, NativeBinding, NativeDestination,
    NativeReading, PostgresTaxStore, RevisionView, SourceField, TaxAccess, validate_field_sources,
)
from .tool_contract import JobScope, Verification
from .tools import ToolBackend


@dataclass(frozen=True)
class WorkItem:
    firm_id: str
    job_id: str
    return_id: str
    input_revision: str
    profile_id: str
    store_id: str
    draft_key: str | None
    identity: DraftIdentity
    snapshot: dict


@dataclass(frozen=True)
class FieldSource:
    """An approved profile maps a native binding to these semantic source fields."""

    fields: tuple[SourceField, ...]
    value: FactValue
    derivation_rule_id: str | None = None


@dataclass(frozen=True)
class WorkerPlan:
    draft: DraftPlan
    sources: Mapping[str, FieldSource]
    transit_contexts: frozenset[str] = frozenset()
    review_issues: tuple[str, ...] = ()
    confirm_completion: bool = False


class WorkerNative(Protocol):
    def session(self, grant: OwnerGrant, item: WorkItem) -> AbstractAsyncContextManager[NativeSession]: ...


class NativeSession(Protocol):
    async def checkout(self) -> str:
        """Return a firm authority's opaque exclusive checkout reference."""
        ...

    async def establish_draft(self, store: OwnershipStore) -> None:
        """Bind an exact existing draft, or journal and verify a permitted Create."""
        ...

    def tools(self, scope: JobScope, plan: DraftPlan) -> AbstractAsyncContextManager[ToolBackend]: ...

    async def close_proof(self) -> str:
        """Verify native close and checkout release independently after the tool process exits."""
        ...


OpenProvider = Callable[[], AbstractAsyncContextManager[Provider]]


@dataclass(frozen=True)
class WorkerEnrollment:
    machine_id: str
    worker_id: str
    policy: StagePolicy
    native: WorkerNative
    lock_path: Path
    plan: Callable[[WorkItem, RevisionView], WorkerPlan]
    authorize: Callable[[WorkItem], Authorization | None]
    open_provider: OpenProvider


@dataclass(frozen=True)
class _MappedField:
    section: str
    record: RecordPlan | None
    field: FieldPlan
    binding: NativeBinding
    state: FieldState


def binding_reference(section: str, source_record_id: str | None, field_key: str) -> str:
    """Stable native binding reference without a raw tax identity in a tool argument."""
    encoded = json.dumps((section, source_record_id, field_key), separators=(",", ":")).encode()
    return f"binding-{sha256(encoded).hexdigest()}"


def _native_text(value: FactValue) -> str:
    if isinstance(value, Amount):
        return format(value.value, "f")
    if isinstance(value, BooleanValue):
        return "true" if value.value else "false"
    if isinstance(value, TextValue):
        return value.value
    return ""


def _validate_plan(
    item: WorkItem, view: RevisionView, plan: WorkerPlan, sums: Mapping[DerivationAddress, ApprovedSum],
) -> dict[str, _MappedField]:
    if plan.draft.identity != item.identity:
        raise PreparationBlocked("plan-return-mismatch")
    mapped = {}
    for section in plan.draft.sections:
        for record, fields in ((None, section.fields), *((row, row.fields) for row in section.records)):
            for field in fields:
                record_id = record.source_record_id if record else None
                if field.binding_id != binding_reference(section.section_key, record_id, field.field_key):
                    raise PreparationBlocked("plan-binding-reference-not-opaque")
                source = plan.sources.get(field.binding_id)
                if source is None or (record is not None and (not source.fields or any(
                    part.record_id != record.source_record_id for part in source.fields
                ))):
                    raise PreparationBlocked("plan-source-record-mismatch")
                if not field.matches(_native_text(source.value)):
                    raise PreparationBlocked("plan-source-value-mismatch")
                binding = NativeBinding(
                    binding_id=field.binding_id, section=section.section_key, field_key=field.field_key,
                    record_key=record.external_key if record else None, source_fields=source.fields,
                )
                old = view.fields.get(field.binding_id)
                previous_saved = old.state.last_saved if old else None
                baseline = _native_text(previous_saved.value) if previous_saved else ""
                if not field._equal(field.expected_saved or "", baseline):
                    raise PreparationBlocked("plan-baseline-mismatch")
                if old and (old.binding != binding or (old.state.reviewer_saved is not None and not field.preparer_owned)):
                    raise PreparationBlocked("plan-native-binding-or-owner-mismatch")
                candidate_ids = tuple(
                    f.fact_id for f in view.facts.values()
                    if any(f.record_id == key.record_id and f.fact_key == key.fact_key for key in source.fields)
                )
                state = FieldState(candidate_ids=candidate_ids, used_candidate_ids=field.fact_ids,
                                   proposed=source.value, derivation_rule_id=source.derivation_rule_id)
                rule = sums.get((item.profile_id, section.section_key, None, field.field_key))
                validate_field_sources(binding, state, view.facts, view.effective_facts, rule)
                mapped[field.binding_id] = _MappedField(section.section_key, record, field, binding, state)
    if not mapped or mapped.keys() != plan.sources.keys():
        raise PreparationBlocked("plan-fields-mismatch")
    return mapped


def _observed(text: str, kind: str) -> FactValue:
    if not text:
        return MissingValue(kind="missing")
    if kind == "decimal":
        return Amount(kind="amount", value=Decimal(text))
    return TextValue(kind="text", value=text)


class PreparationWorker:
    def __init__(
        self, db: CoordinatorDB, ownership: OwnershipStore, tax: PostgresTaxStore, *,
        firm_id: str, enrollment: WorkerEnrollment, approved_sums: Mapping[DerivationAddress, ApprovedSum],
    ):
        if enrollment.policy.stage != "reasoning":
            raise ValueError("A preparation worker requires an enrolled reasoning policy.")
        self.db, self.ownership, self.tax = db, ownership, tax
        self.firm_id, self.enrollment, self.approved_sums = firm_id, enrollment, dict(approved_sums)

    def _candidates(self, limit: int) -> tuple[WorkItem, ...]:
        with self.db.transaction() as connection:
            rows = connection.execute("""
                SELECT job.firm_id, job.job_id, job.return_id, job.input_revision, job.profile_id,
                       target.store_id, target.draft_key, target.tax_year, directory.client_reference,
                       directory.return_type, job.snapshot
                FROM prep_jobs job JOIN prep_job_outbox USING (firm_id,job_id)
                JOIN prep_returns target USING (firm_id,return_id)
                JOIN prep_return_directory directory USING (firm_id,return_id)
                JOIN prep_machine_profiles machine ON (
                    machine.firm_id, machine.profile_id, machine.tax_year, machine.store_id
                )=(job.firm_id,job.profile_id,target.tax_year,target.store_id)
                JOIN prep_machines seat ON (seat.firm_id,seat.machine_id)=(machine.firm_id,machine.machine_id)
                WHERE job.firm_id=%s AND job.state='queued' AND machine.machine_id=%s AND seat.worker_id=%s
                  AND seat.available AND NOT seat.cordoned AND target.profile_id=job.profile_id
                  AND (target.pinned_machine_id IS NULL OR target.pinned_machine_id=machine.machine_id)
                  AND NOT EXISTS (SELECT 1 FROM prep_owners o WHERE o.firm_id=job.firm_id
                        AND (o.return_id=job.return_id OR o.machine_id=machine.machine_id) AND o.released_at IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM prep_write_blocks b WHERE (b.firm_id,b.return_id)=(job.firm_id,job.return_id))
                  AND NOT EXISTS (SELECT 1 FROM prep_jobs earlier WHERE (earlier.firm_id,earlier.return_id)=(job.firm_id,job.return_id)
                        AND earlier.revision_sequence < job.revision_sequence AND earlier.state IN ('queued','preparing','verifying','reconciling'))
                ORDER BY job.created_at,job.job_id LIMIT %s
            """, (self.firm_id, self.enrollment.machine_id, self.enrollment.worker_id, limit)).fetchall()
        return tuple(WorkItem(
            row["firm_id"], row["job_id"], row["return_id"], row["input_revision"], row["profile_id"],
            row["store_id"], row["draft_key"], DraftIdentity(client_reference=row["client_reference"],
                tax_year=row["tax_year"], return_type=row["return_type"]), row["snapshot"],
        ) for row in rows)

    def _reserve(self, grant: OwnerGrant, policy: StagePolicy) -> None:
        price = (policy.max_input_tokens * Fraction(policy.input_price_per_million)
                 + policy.max_output_tokens * Fraction(policy.output_price_per_million))
        limit = min(policy.max_requests, (1_000_000 * Fraction(policy.max_spend)) // price) if price else policy.max_requests
        if limit < 1:
            raise PreparationBlocked("model-budget-exhausted")
        with self.db.transaction() as connection:
            owner = connection.execute("""
                SELECT 1 FROM prep_owners WHERE (firm_id,return_id,generation,job_id)=(%s,%s,%s,%s)
                  AND released_at IS NULL AND NOT reconciling AND lease_until > clock_timestamp() FOR UPDATE
            """, (grant.firm_id, grant.return_id, grant.generation, grant.job_id)).fetchone()
            if owner is None:
                raise PreparationBlocked("owner-not-dispatchable")
            reserved = connection.execute("""
                INSERT INTO prep_reasoning_usage (firm_id,job_id,reservations) VALUES (%s,%s,1)
                ON CONFLICT (firm_id,job_id) DO UPDATE SET reservations=prep_reasoning_usage.reservations+1
                WHERE prep_reasoning_usage.reservations < %s RETURNING reservations
            """, (grant.firm_id, grant.job_id, limit)).fetchone()
            if reserved is None:
                raise PreparationBlocked("model-budget-exhausted")

    async def run_once(self) -> str | None:
        """Handle one eligible return. Busy seats and held returns are not claimed."""
        items = await asyncio.to_thread(self._candidates, 100)
        if not items:
            return None
        try:
            with hold_session(self.enrollment.lock_path):
                for item in items:
                    duration = int(self.enrollment.policy.timeout_seconds) + 240
                    grant = await asyncio.to_thread(
                        self.ownership.claim_worker, item.firm_id, item.job_id,
                        self.enrollment.machine_id, self.enrollment.worker_id, lease_seconds=min(duration, 86_400),
                    )
                    if grant:
                        await self._run(item, grant)
                        return item.job_id
        except SessionBusy:
            return None
        return None

    async def _run(self, item: WorkItem, grant: OwnerGrant) -> None:
        try:
            async with self.enrollment.native.session(grant, item) as session:
                issue = None
                try:
                    await asyncio.to_thread(self.ownership.activate, grant, await session.checkout())
                    async with asyncio.timeout(self.enrollment.policy.timeout_seconds + 180):
                        await self._prepare(item, grant, session)
                except PreparationBlocked as exc:
                    code = str(exc)
                    issue = code if code and all(c.isascii() and (c.islower() or c.isdigit() or c == "-") for c in code) else "preparation-blocked"
                except (TimeoutError, OwnershipBlocked):
                    issue = "native-preparation-unconfirmed"
                except Exception:
                    issue = "worker-failed"
                await asyncio.to_thread(self.ownership.release, grant, await session.close_proof(), issue=issue)
        except asyncio.CancelledError:
            with suppress(OwnershipBlocked):
                await asyncio.shield(asyncio.to_thread(self.ownership.interrupt, grant, "disconnected"))
            raise
        except Exception:
            with suppress(OwnershipBlocked):
                await asyncio.to_thread(self.ownership.interrupt, grant, "uncertain")

    async def _prepare(self, item: WorkItem, grant: OwnerGrant, session: NativeSession) -> None:
        try:
            policy = StagePolicy.from_wire(item.snapshot["reasoning_policy"])
            inputs = item.snapshot["inputs"]
            revision = max(int(source["artifact_id"]) for source in inputs)
            classes = frozenset(source["extraction_policy"]["data_class"] for source in inputs)
        except (KeyError, ValueError, TypeError):
            raise PreparationBlocked("invalid-frozen-job-policy") from None
        authorization = await asyncio.to_thread(self.enrollment.authorize, item)
        if (
            policy != self.enrollment.policy or not authorization or authorization.identity != policy.identity
            or authorization.revision != item.input_revision or authorization.stage != "reasoning"
            or authorization.expires_at.utcoffset() is None or authorization.expires_at <= datetime.now(timezone.utc)
            or not classes or not classes <= policy.document_classes or not classes <= authorization.document_classes
        ):
            raise PreparationBlocked("frozen-job-policy-not-authorized")

        access = TaxAccess(firm_id=item.firm_id, return_id=item.return_id, actor_id=grant.actor_id,
                           scopes=frozenset({"read", "prepare"}), active_revision=revision)
        view = await asyncio.to_thread(self.tax.read_revision, access, revision)
        origins = {(pointer.original.document_id, pointer.original.version_id, pointer.storage_ref, pointer.digest)
                   for pointer in view.originals}
        if any((source["upload_id"], source["version_id"], source["object_id"], source["sha256"]) not in origins
               for source in inputs):
            raise PreparationBlocked("frozen-job-artifact-mismatch")
        plan = await asyncio.to_thread(self.enrollment.plan, item, view)
        if any(not re.fullmatch(r"[a-z][a-z0-9-]{0,99}", issue) for issue in plan.review_issues):
            raise PreparationBlocked("plan-review-issues-invalid")
        mapped = _validate_plan(item, view, plan, self.approved_sums)

        await session.establish_draft(self.ownership)
        scope = await asyncio.to_thread(self.ownership.scope, grant)
        contexts = plan.transit_contexts | {section.section_key for section in plan.draft.sections}
        assignment = DraftAssignment(
            scope, item.input_revision, classes, contexts, contexts | {field.field.field_key for field in mapped.values()},
            tuple((binding_id, field.field.fact_ids) for binding_id, field in mapped.items()),
            frozenset(field.record.source_record_id for field in mapped.values() if field.record), policy.max_requests, 3,
            review_issues=plan.review_issues,
            confirm_completion=plan.confirm_completion,
        )

        def record(connection, result: Verification) -> None:
            if set(result.verified_binding_ids) - {item.binding_id for item in result.readbacks}:
                raise OwnershipBlocked("reopen-values-missing")
            updates = []
            now = datetime.now(timezone.utc)
            for reading in result.readbacks:
                target = mapped.get(reading.binding_id)
                if target is None or (reading.section_key, reading.field_key, reading.value_kind, reading.fact_ids) != (
                    target.section, target.field.field_key, target.field.kind, target.field.fact_ids,
                ):
                    raise OwnershipBlocked("reopen-binding-mismatch")
                record_id = target.record.source_record_id if target.record else None
                if reading.source_record_id != record_id or (
                    target.binding.record_key is not None and reading.native_row_key != target.binding.record_key
                ):
                    raise OwnershipBlocked("reopen-record-mismatch")
                if reading.normalized_value is None:
                    continue
                matches = target.field.matches(reading.normalized_value)
                if matches != reading.matches_intended:
                    raise OwnershipBlocked("reopen-value-mismatch")
                value = target.state.proposed if matches else _observed(reading.normalized_value, reading.value_kind)
                updates.append(FieldUpdate(
                    binding=target.binding.model_copy(update={"record_key": reading.native_row_key}),
                    state=target.state.model_copy(update={"reopened": NativeReading(value=value, observed_at=now)}),
                ))
            with self.tax.using_connection(connection) as store:
                store.record_fields(access, revision, view.field_version,
                    destination=NativeDestination(profile_id=scope.profile_id, draft_key=scope.draft_key), updates=updates)

        journal = OwnershipJournal(self.ownership, grant, persist_verification=record)

        async def reserve() -> None:
            current = await asyncio.to_thread(self.enrollment.authorize, item)
            if current != authorization:
                raise PreparationBlocked("frozen-job-policy-not-authorized")
            await journal.require_owner(scope)
            await asyncio.to_thread(self._reserve, grant, policy)

        async with session.tools(scope, plan.draft) as tools, self.enrollment.open_provider() as provider:
            await prepare(assignment, provider=provider, policy=policy, grant=authorization,
                          host=tools, journal=journal, reserve_request=reserve)
