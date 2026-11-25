"""Firm-side preparer API. Native checkout authority stays in OwnershipStore."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from psycopg.rows import DictRow
from pydantic import AwareDatetime, Field, StrictBool, StrictInt, TypeAdapter, ValidationError

from ..review_documents import MAX_PDF_BYTES, DocumentError, inspect_pdf
from ..review_store import ReviewError
from .batches import BatchIntake, BatchManifest, BatchView, ClientAssignment, ImmutableObjects, IngestScope, IntakeClient
from .contracts import Amount, BooleanValue, Contract, FactValue, OriginalRef, Text, TextValue
from .coordinator_db import CoordinatorDB
from .dispatch import ExtractionPolicy
from .ownership import OwnerGrant, OwnershipBlocked, OwnershipStore
from .review_routes import (
    EvidenceCandidate, EvidenceCitation, EvidenceDetail, EvidenceDocument, EvidenceField,
    ReviewDecision, ReviewDecisionRecorded, RevisionLink,
)
from .sigma_config import IntakeSettings
from .tax_store import (
    ChooseSource, CitedFact, MarkReviewed, NativeDestination, OriginalPointer, PostgresTaxStore,
    RevisionView, TaxAccess, TaxAccessDenied, TaxConflict, TaxNotFound,
)
from .tool_contract import Ref


@dataclass(frozen=True, slots=True)
class Principal:
    """The result of firm SSO, preparer assignment, and device/profile authorization."""

    firm_id: str
    actor_id: str
    return_ids: frozenset[str]
    machine_id: str
    profile_id: str


Authenticate = Callable[[str, str, str], Principal | None]
Generation = Annotated[StrictInt, Field(gt=0)]
_ReservationStatus = Literal['queued', 'reserved', 'active', 'recovery_required', 'closed', 'cancelled']
_BEARER = re.compile(r'Bearer ([A-Za-z0-9_.~+/=-]{16,8192})', re.ASCII | re.IGNORECASE)
_KEY = re.compile(r'[A-Za-z0-9_.:-]{1,120}', re.ASCII)
_TAX_REVISION = re.compile(r'[1-9][0-9]{0,9}', re.ASCII)
_OBSERVATION_WINDOW = timedelta(hours=8)
_REF = TypeAdapter(Ref)
_TERMINAL = frozenset({'ready_for_review', 'needs_attention'})


class _ReservationRequest(Contract):
    input_revision: Ref
    queue_only: StrictBool = False


class _ReservationProof(Contract):
    generation: Generation
    proof_id: Text


class _ObserverBinding(Contract):
    section: Ref
    record: Ref | None
    field: Ref
    binding_id: Ref


class _Reservation(Contract):
    review_id: Ref
    firm_id: Ref
    return_id: Ref
    preparer_id: Ref
    machine_id: Ref
    profile_id: Ref
    input_revision: Ref
    status: _ReservationStatus
    generation: Generation | None
    draft_key: Text | None
    expires_at: AwareDatetime | None
    bindings: tuple[_ObserverBinding, ...]
    client_key: Text
    tax_year: int
    return_type: Literal['individual', 'business', '1120']
    product: Literal['web', 'desktop']


_DIRECTORY = """SELECT r.return_id, r.profile_id, r.tax_year, r.store_id, r.draft_key,
                       r.pinned_machine_id, r.review_required, d.client_id, d.client_name,
                       d.client_reference, d.return_type, d.product, d.updated_at
                FROM prep_returns r JOIN prep_return_directory d USING (firm_id, return_id)
                WHERE r.firm_id = %s AND r.profile_id = %s"""
_JOB_ORDER = 'revision_sequence DESC, created_at DESC, job_id DESC'
_REVIEW = """SELECT q.*, o.generation, o.checkout_proof_id, o.reconciling, o.released_at,
                    (SELECT min(e.created_at) FROM prep_owner_events e
                      WHERE (e.firm_id, e.return_id, e.generation) = (o.firm_id, o.return_id, o.generation)
                        AND e.event = 'claimed') AS claimed_at
             FROM prep_review_requests q LEFT JOIN prep_owners o USING (firm_id, return_id, review_id)
             WHERE (q.firm_id, q.return_id, q.profile_id, q.machine_id, q.reviewer_id)
                   = (%s, %s, %s, %s, %s)"""
_FORM_LABELS = {
    'w2': 'W-2', 'w2c': 'W-2c', '1099k': '1099-K', '1099nec': '1099-NEC',
    '1099_other': '1099', 'platform_summary': 'Platform summary', 'receipt': 'Receipt',
    'business_ledger': 'Business ledger',
}
_COLUMN_LABELS = {'previously_reported': 'Previously reported', 'correct_information': 'Corrected information'}


def _tax_revision(job: DictRow) -> int:
    """Tax revisions are cumulative; the greatest pinned artifact includes the earlier inputs."""
    snapshot = job['snapshot']
    inputs = snapshot.get('inputs') if isinstance(snapshot, dict) else None
    if not isinstance(inputs, list) or not inputs:
        raise ReviewError(409, 'This preparation job has no frozen tax evidence.')
    revisions = []
    for item in inputs:
        artifact = item.get('artifact_id') if isinstance(item, dict) else None
        if not isinstance(artifact, str) or not _TAX_REVISION.fullmatch(artifact) or int(artifact) > 2_147_483_647:
            raise ReviewError(409, 'The preparation job does not identify an exact tax snapshot.')
        revisions.append(int(artifact))
    return max(revisions)


def _label(key: str) -> str:
    return re.sub(r'[-_.:]+', ' ', key).strip().capitalize() or key


def _display(value: FactValue | None) -> str | None:
    if isinstance(value, Amount):
        return format(value.value, 'f')
    if isinstance(value, BooleanValue):
        return 'Yes' if value.value else 'No'
    if isinstance(value, TextValue):
        return value.value
    if value is not None:
        return '' if value.kind == 'missing' else 'Not applicable'
    return None


def _record_label(view: RevisionView, record_id: str) -> str:
    record = view.records[record_id].identity
    parts = [record.issuer_reference, record.activity_reference, record.instance_reference]
    return ' · '.join([_FORM_LABELS[record.form], *(part for part in parts if part)])


def _project_fields(view: RevisionView, titles: dict[OriginalRef, str] | None = None) -> tuple[EvidenceField, ...]:
    titles = titles or {}
    projected = []
    linked: set[str] = set()
    chosen = {
        entry.decision.binding_id: entry.decision.candidate_id
        for entry in view.decisions if isinstance(entry.decision, ChooseSource)
    }

    def candidate(fact: CitedFact) -> EvidenceCandidate:
        return EvidenceCandidate(
            id=fact.fact_id, value=_display(fact.normalized),
            label=_COLUMN_LABELS.get(fact.column, _FORM_LABELS[view.records[fact.record_id].identity.form]),
            citations=tuple(EvidenceCitation(
                id=sha256(f'{fact.fact_id}:{index}'.encode()).hexdigest(),
                label=titles.get(anchor.original, 'Original'), evidence=anchor,
            ) for index, anchor in enumerate(fact.citations)),
        )

    for binding_id, item in sorted(view.fields.items()):
        binding, state = item.binding, item.state
        facts = [view.facts[fact_id] for fact_id in state.candidate_ids]
        linked.update(state.candidate_ids)
        selected = chosen.get(binding_id)
        candidates = tuple(map(candidate, facts))
        # A later source choice records evidence preference; it never substitutes for the citation actually used.
        used = next((citation.id for fact_id in state.used_candidate_ids for option in candidates
                     if option.id == fact_id for citation in option.citations
                     if citation.evidence.precision != 'document'), None)
        preferred = next((fact for fact in facts if fact.fact_id == selected), None)
        proposed = state.proposed
        saved = state.last_saved
        reviewer = saved is not None and saved is state.reviewer_saved
        newest = next((fact for fact in facts if fact.column == 'correct_information'), None)
        predecessor = view.facts.get(newest.predecessor_fact_id) if newest and newest.predecessor_fact_id else None
        if predecessor is not None and predecessor.fact_id not in state.candidate_ids:
            candidates += (candidate(predecessor),)
            linked.add(predecessor.fact_id)
        comparison = [fact for fact in facts if fact.column != 'previously_reported' and fact.eligible]
        conflicting = (saved is not None and not reviewer and proposed is not None and saved.value != proposed
                       or not state.used_candidate_ids and preferred is None
                       and any(fact.normalized != comparison[0].normalized for fact in comparison[1:]))
        if conflicting:
            status = 'conflict'
        elif (saved is not None and saved.value.kind not in {'missing', 'not_applicable'}
              and (reviewer or proposed is not None and saved.value == proposed)):
            status = 'verified'
        elif not facts and saved is not None and saved.value.kind == 'missing':
            status = 'needs_client_input'
        else:
            status = 'unverified'
        current = saved or state.visible or state.entered
        label_records = dict.fromkeys(_record_label(view, source.record_id) for source in binding.source_fields)
        projected.append(EvidenceField(
            id=binding_id, label=_label(binding.field_key), record_label=' · '.join(label_records)
                or 'Sigma · ' + _label(binding.subform or binding.section),
            value=_display(current.value if current else proposed), saved_value=_display(saved.value if saved else None),
            proposed_value=_display(proposed if proposed is not None else newest.normalized if newest else None),
            prior_source_value=_display(predecessor.normalized if predecessor else None),
            provenance='preparer' if reviewer else 'source' if state.used_candidate_ids else 'none',
            status=status, used_citation_id=used, candidates=candidates, derivation=state.derivation,
        ))

    by_field = defaultdict(list)
    for fact in view.facts.values():
        by_field[fact.record_id, fact.fact_key].append(fact)
    for (record_id, fact_key), all_facts in sorted(by_field.items()):
        if all(fact.fact_id in linked for fact in all_facts):
            continue
        facts = sorted(all_facts, key=lambda fact: fact.fact_id)
        eligible = [fact.normalized for fact in facts if fact.eligible and fact.column != 'previously_reported']
        conflict = any(value != eligible[0] for value in eligible[1:])
        first = eligible[0] if eligible and not conflict else None
        field_id = 'source-' + sha256(f'{record_id}\0{fact_key}'.encode()).hexdigest()
        projected.append(EvidenceField(
            id=field_id, label=_label(fact_key), record_label='Source only · ' + _record_label(view, record_id),
            value=_display(first), provenance='source', status='conflict' if conflict else 'unverified',
            candidates=tuple(map(candidate, facts)),
        ))
    return tuple(projected)


class _FirmData:
    def __init__(self, db: CoordinatorDB, tax: PostgresTaxStore, objects: ImmutableObjects,
                 ownership: OwnershipStore) -> None:
        self.db = db
        self.tax = tax
        self.objects = objects
        self.ownership = ownership

    @staticmethod
    def target(conn, principal: Principal, return_id: str, *, lock: bool = False) -> DictRow:
        if return_id not in principal.return_ids:
            raise ReviewError(404, 'Return not found in this assignment.')
        row = conn.execute(_DIRECTORY + ' AND r.return_id = %s' + (' FOR UPDATE OF r' if lock else ''),
                           (principal.firm_id, principal.profile_id, return_id)).fetchone()
        if row is None:
            raise ReviewError(404, 'Return not found in this assignment.')
        return row

    def authorize(self, principal: Principal, return_id: str) -> None:
        with self.db.transaction() as conn:
            self.target(conn, principal, return_id)

    def intake_clients(self, principal: Principal) -> tuple[IntakeClient, ...]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                _DIRECTORY + ' AND r.return_id = ANY(%s) ORDER BY d.client_name, d.client_id, r.tax_year DESC',
                (principal.firm_id, principal.profile_id, sorted(principal.return_ids)),
            ).fetchall()
        clients = {}
        for row in rows:
            name, years = clients.setdefault(row['client_id'], (row['client_name'], set()))
            years.add(row['tax_year'])
        return tuple(IntakeClient(client_id=client_id, name=name, tax_years=tuple(sorted(years, reverse=True)))
                     for client_id, (name, years) in clients.items())

    def resolve_assignment(
        self, principal: Principal, assignment: ClientAssignment | None, hint: str | None,
    ) -> tuple[ClientAssignment, str]:
        """Resolve an intake scope from the preparer's firm roster, never from a display name."""
        with self.db.transaction() as conn:
            if hint is not None:
                row = self.target(conn, principal, hint)
            elif assignment is not None:
                rows = conn.execute(
                    _DIRECTORY + ' AND d.client_id=%s AND r.tax_year=%s AND r.return_id=ANY(%s) LIMIT 2',
                    (principal.firm_id, principal.profile_id, assignment.client_id, assignment.tax_year,
                     sorted(principal.return_ids)),
                ).fetchall()
                if not rows:
                    raise ReviewError(404, 'No authorized return found for this client and tax year.')
                if len(rows) > 1:
                    raise ReviewError(409, 'More than one return matches. Upload from the intended return workspace.')
                row, = rows
            else:
                raise ReviewError(422, 'Choose a client and tax year before uploading.')

        target = ClientAssignment(client_id=row['client_id'], tax_year=row['tax_year'])
        if assignment is not None and assignment != target:
            raise ReviewError(409, 'The client and tax year do not match this return.')
        return target, row['return_id']

    @staticmethod
    def tax_access(principal: Principal, return_id: str, review: int | None = None) -> TaxAccess:
        return TaxAccess(firm_id=principal.firm_id, actor_id=principal.actor_id, return_id=return_id,
                         scopes=frozenset({'read'} if review is None else {'review'}), active_revision=review)

    @staticmethod
    def job_rows(conn, principal: Principal, return_id: str) -> list[DictRow]:
        return conn.execute(f"""SELECT job_id, input_revision, state, snapshot, created_at
                               FROM prep_jobs WHERE (firm_id, return_id, profile_id) = (%s, %s, %s)
                               ORDER BY {_JOB_ORDER}""", (principal.firm_id, return_id, principal.profile_id)).fetchall()

    @staticmethod
    def select_job(jobs: list[DictRow], revision: str | None) -> DictRow:
        row = jobs[0] if revision is None and jobs else next(
            (job for job in jobs if job['input_revision'] == revision), None,
        )
        if row is None:
            raise ReviewError(404, 'No frozen evidence exists for this return revision.')
        return row

    def returns(self, principal: Principal) -> list[dict]:
        with self.db.transaction() as conn:
            targets = conn.execute(_DIRECTORY + ' AND r.return_id = ANY(%s) ORDER BY d.client_name, r.tax_year DESC, r.return_id',
                                   (principal.firm_id, principal.profile_id, sorted(principal.return_ids))).fetchall()
            result = []
            with self.tax.using_connection(conn) as tax:
                for target in targets:
                    return_id = target['return_id']
                    job = conn.execute(f"""SELECT snapshot, created_at FROM prep_jobs
                                           WHERE (firm_id, return_id, profile_id) = (%s, %s, %s)
                                           ORDER BY {_JOB_ORDER} LIMIT 1""",
                                       (principal.firm_id, return_id, principal.profile_id)).fetchone()
                    fields = ()
                    if job is not None:
                        try:
                            fields = _project_fields(tax.read_revision(self.tax_access(principal, return_id), _tax_revision(job)))
                        except (TaxNotFound, ReviewError):
                            # An unavailable snapshot still has a return and a job status, but no evidence counts.
                            pass
                    result.append({
                        'id': return_id, 'clientId': target['client_id'], 'clientName': target['client_name'],
                        'taxYear': target['tax_year'], 'returnType': target['return_type'],
                        'destination': 'Sigma Web' if target['product'] == 'web' else 'Sigma Desktop',
                        'totalFields': len(fields), 'acceptedFields': sum(field.status == 'verified' for field in fields),
                        'conflictFields': sum(field.status == 'conflict' for field in fields),
                        'missingFields': sum(field.status == 'needs_client_input' for field in fields),
                        'updatedAt': max(target['updated_at'], job['created_at']).isoformat() if job else target['updated_at'].isoformat(),
                        'reviewRequired': target['review_required'],
                    })
            return result

    def jobs(self, principal: Principal, return_id: str) -> list[dict]:
        with self.db.transaction() as conn:
            self.target(conn, principal, return_id)
            jobs = conn.execute(f"""SELECT j.job_id, j.input_revision, j.state, v.issues
                                   FROM prep_jobs j LEFT JOIN LATERAL (
                                     SELECT result -> 'issues' AS issues FROM prep_verifications
                                     WHERE (firm_id, return_id, job_id) = (j.firm_id, j.return_id, j.job_id)
                                     ORDER BY generation DESC LIMIT 1
                                   ) v ON true
                                   WHERE (j.firm_id, j.return_id, j.profile_id) = (%s, %s, %s)
                                   ORDER BY {', '.join('j.' + item for item in _JOB_ORDER.split(', '))}""",
                                (principal.firm_id, return_id, principal.profile_id)).fetchall()
        result = []
        for job in jobs:
            issues = job['issues'] or []
            if job['state'] == 'needs_attention' and not issues:
                issues = ['Preparation stopped before verification; inspect the return or contact the firm administrator.']
            if job['state'] == 'reconciling':
                issues = ['The previous native session must be reconciled before work can resume.']
            result.append({'id': job['job_id'], 'returnId': return_id, 'inputRevision': job['input_revision'],
                           'status': job['state'], 'issues': issues})
        return result

    def original_bytes(self, firm_id: str, pointer: OriginalPointer) -> bytes:
        content = self.objects.read(firm_id, pointer.storage_ref)
        if (not isinstance(content, bytes) or not content or len(content) > MAX_PDF_BYTES
                or sha256(content).hexdigest() != pointer.digest):
            raise ReviewError(503, 'The protected original is unavailable or failed its integrity check.')
        return content

    def evidence(self, principal: Principal, return_id: str, revision: str | None) -> EvidenceDetail:
        with self.db.transaction() as conn, self.tax.using_connection(conn) as tax:
            target = self.target(conn, principal, return_id)
            jobs = self.job_rows(conn, principal, return_id)
            job = self.select_job(jobs, revision)
            view = tax.read_revision(self.tax_access(principal, return_id), _tax_revision(job))
            titles = {
                OriginalRef(document_id=row['upload_id'], version_id=row['version_id']): row['filename']
                for row in conn.execute("""SELECT s.upload_id, s.version_id, s.filename FROM prep_upload_slots s
                    JOIN prep_dispatch_uploads d USING (firm_id, upload_id, version_id)
                    WHERE (d.firm_id, d.return_id) = (%s, %s) AND d.state <> 'misassigned'
                      AND s.upload_id = ANY(%s)""", (principal.firm_id, return_id,
                          [pointer.original.document_id for pointer in view.originals]))
            }
            qualified = conn.execute("""SELECT 1 FROM prep_machine_profiles p JOIN prep_machines m USING (firm_id, machine_id)
                    WHERE (p.firm_id, p.machine_id, p.profile_id, p.tax_year, p.store_id) = (%s, %s, %s, %s, %s)
                      AND NOT m.cordoned AND (p.shared_store OR %s::text IS NULL OR %s::text = p.machine_id)""",
                (principal.firm_id, principal.machine_id, principal.profile_id, target['tax_year'], target['store_id'],
                 target['draft_key'], target['pinned_machine_id'])).fetchone()
            held = self.held_review(conn, principal, return_id)
        docs = []
        for index, pointer in enumerate(view.originals, 1):
            original = pointer.original
            title = titles.setdefault(original, f'Original {index}')
            pages = None
            if pointer.media_type == 'application/pdf':
                content = self.original_bytes(principal.firm_id, pointer)
                try:
                    pages = inspect_pdf(content)['pageCount']
                except DocumentError:
                    raise ReviewError(503, 'The protected PDF cannot be inspected.') from None
            docs.append(EvidenceDocument(id=original.document_id, version_id=original.version_id, title=title,
                                         media_type=pointer.media_type, page_count=pages))
        links = tuple(RevisionLink(id=item['input_revision'], label=f'Revision {number}')
                      for number, item in enumerate(reversed(jobs), 1))
        available = bool(held or (qualified and (target['draft_key'] or job['state'] not in _TERMINAL)))
        return EvidenceDetail(
            id=return_id, client_id=target['client_id'], client_name=target['client_name'], tax_year=target['tax_year'],
            return_type=target['return_type'], input_revision=job['input_revision'], latest_revision=jobs[0]['input_revision'],
            revisions=links, documents=tuple(docs), fields=_project_fields(view, titles), review_available=available,
            review_message=None if available else 'This machine is not qualified to open a saved native draft for this return.',
            field_version=view.field_version, decision_version=view.decision_version,
        )

    def original(self, principal: Principal, return_id: str, revision: str, original: OriginalRef) -> Response:
        with self.db.transaction() as conn, self.tax.using_connection(conn) as tax:
            self.target(conn, principal, return_id)
            job = self.select_job(self.job_rows(conn, principal, return_id), revision)
            pointer = tax.get_original(self.tax_access(principal, return_id), _tax_revision(job), original)
        return Response(self.original_bytes(principal.firm_id, pointer), media_type=pointer.media_type)

    @staticmethod
    def held_review(conn, principal: Principal, return_id: str) -> DictRow | None:
        return conn.execute(
            _REVIEW + " AND q.state='owned' AND o.generation IS NOT NULL AND o.released_at IS NULL",
            (principal.firm_id, return_id, principal.profile_id, principal.machine_id, principal.actor_id),
        ).fetchone()

    @staticmethod
    def review_row(conn, principal: Principal, return_id: str, *, review_id: str | None = None,
                   input_revision: str | None = None) -> DictRow:
        scope = (principal.firm_id, return_id, principal.profile_id, principal.machine_id, principal.actor_id)
        if review_id is not None:
            row = conn.execute(_REVIEW + ' AND q.review_id = %s', (*scope, review_id)).fetchone()
        else:
            row = conn.execute(_REVIEW + """ AND q.input_revision = %s AND q.state IN ('pending', 'owned')
                                   ORDER BY (q.state = 'owned') DESC, q.request_order LIMIT 1""",
                               (*scope, input_revision)).fetchone()
        if row is None:
            raise ReviewError(404, 'Review reservation not found in this assignment.')
        return row

    @staticmethod
    def grant(row: DictRow) -> OwnerGrant:
        if row['state'] != 'owned' or row['generation'] is None or row['released_at'] is not None:
            raise ReviewError(409, 'The review no longer holds the central reservation.')
        return OwnerGrant(row['firm_id'], row['return_id'], row['generation'], row['machine_id'],
                          row['reviewer_id'], None, row['review_id'])

    @staticmethod
    def expiry(row: DictRow) -> datetime | None:
        return row['claimed_at'] + _OBSERVATION_WINDOW if row['claimed_at'] is not None else None

    def reservation(self, principal: Principal, return_id: str, *, review_id: str | None = None,
                    input_revision: str | None = None, new_grant: OwnerGrant | None = None,
                    allow_reopen: bool = True) -> _Reservation:
        with self.db.transaction() as conn, self.tax.using_connection(conn) as tax:
            target = self.target(conn, principal, return_id)
            row = self.review_row(conn, principal, return_id, review_id=review_id, input_revision=input_revision)
            expires = self.expiry(row)
            state: _ReservationStatus
            draft = None
            bindings = ()
            if row['state'] == 'owned':
                grant = self.grant(row)
                draft = target['draft_key']
                if (not draft or row['reconciling'] or expires is None or expires <= datetime.now(timezone.utc)
                        or not allow_reopen and grant != new_grant):
                    state = 'recovery_required'
                elif row['checkout_proof_id'] is not None:
                    state = 'active'
                else:
                    state = 'reserved'
                if state in ('active', 'reserved'):
                    job = self.select_job(self.job_rows(conn, principal, return_id), row['input_revision'])
                    view = tax.read_revision(self.tax_access(principal, return_id), _tax_revision(job))
                    if view.destination == NativeDestination(profile_id=principal.profile_id, draft_key=draft):
                        bindings = self.bindings(view)
            elif row['state'] == 'pending':
                state = 'queued'
            else:
                state = 'closed' if row['state'] == 'closed' else 'cancelled'
        return _Reservation(
            review_id=row['review_id'], firm_id=principal.firm_id, return_id=return_id, preparer_id=principal.actor_id,
            machine_id=principal.machine_id, profile_id=principal.profile_id, input_revision=row['input_revision'],
            status=state, generation=row['generation'], draft_key=draft, expires_at=expires, bindings=bindings,
            client_key=target['client_reference'], tax_year=target['tax_year'], return_type=target['return_type'],
            product=target['product'],
        )

    @staticmethod
    def bindings(view: RevisionView) -> tuple[_ObserverBinding, ...]:
        addresses = defaultdict(list)
        for item in view.fields.values():
            binding = item.binding
            try:
                record = _REF.validate_python(binding.record_key) if binding.record_key is not None else None
            except ValidationError:
                continue  # Observer reference syntax cannot identify this native record.
            addresses[binding.subform or binding.section, record, binding.field_key].append(binding.binding_id)
        return tuple(_ObserverBinding(section=section, record=record, field=field, binding_id=ids[0])
                     for (section, record, field), ids in addresses.items() if len(ids) == 1)

    def claim(self, principal: Principal, return_id: str, review_id: str, *, queue_only: bool = False) -> _Reservation:
        with self.db.transaction() as conn:
            self.target(conn, principal, return_id)
            row = self.review_row(conn, principal, return_id, review_id=review_id)
        grant = None
        if row['state'] == 'pending' and not queue_only:
            grant = self.ownership.claim_review(principal.firm_id, return_id, review_id, principal.actor_id)
        return self.reservation(principal, return_id, review_id=review_id, new_grant=grant, allow_reopen=False)

    def proof(self, principal: Principal, return_id: str, review_id: str, body: _ReservationProof, *, close: bool) -> _Reservation:
        with self.db.transaction() as conn:
            self.target(conn, principal, return_id)
            row = self.review_row(conn, principal, return_id, review_id=review_id)
            if row['generation'] != body.generation:
                raise ReviewError(409, 'The review reservation generation changed.')
            if close and row['state'] == 'closed':
                replay = conn.execute("""SELECT 1 FROM prep_owner_events WHERE (firm_id, return_id, generation) =
                                         (%s, %s, %s) AND event = 'released' AND proof_id = %s""",
                                      (principal.firm_id, return_id, body.generation, body.proof_id)).fetchone()
                if replay is None:
                    raise ReviewError(409, 'The review reservation is already closed.')
                grant = None
            else:
                grant = self.grant(row)
                if not close and (self.expiry(row) is None or self.expiry(row) <= datetime.now(timezone.utc)):
                    raise ReviewError(409, 'The observation grant has expired; close the native review.')
        if grant is not None:
            if close:
                self.ownership.release(grant, body.proof_id)
            else:
                self.ownership.activate(grant, body.proof_id)
        return self.reservation(principal, return_id, review_id=review_id)

    def decision(self, principal: Principal, return_id: str, command: ReviewDecision) -> ReviewDecisionRecorded:
        with self.db.transaction() as conn, self.tax.using_connection(conn) as tax:
            self.target(conn, principal, return_id, lock=True)
            row = self.review_row(conn, principal, return_id, review_id=command.review_session_id)
            grant = self.grant(row)
            expires = self.expiry(row)
            if row['input_revision'] != command.input_revision or expires is None or expires <= datetime.now(timezone.utc):
                raise ReviewError(409, 'The decision does not match the active observation grant.')
            self.ownership.require_review_checkout(conn, grant)
            jobs = self.job_rows(conn, principal, return_id)
            job = self.select_job(jobs, command.input_revision)
            revision = _tax_revision(job)
            change = (ChooseSource(kind='choose_source', binding_id=command.binding_id, candidate_id=command.candidate_id)
                      if command.kind == 'choose_source' else MarkReviewed(kind='mark_reviewed'))
            version = tax.record_decision(self.tax_access(principal, return_id, revision), revision,
                                          command.expected_field_version, command.expected_decision_version, change)
            if isinstance(change, MarkReviewed) and job['job_id'] == jobs[0]['job_id']:
                conn.execute("""UPDATE prep_returns r SET review_required = false
                               WHERE (r.firm_id, r.return_id) = (%s, %s)
                                 AND EXISTS (SELECT 1 FROM tax_returns t WHERE (t.firm_id, t.return_id) =
                                     (r.firm_id, r.return_id) AND t.latest_revision = %s)
                                 AND NOT EXISTS (SELECT 1 FROM prep_dispatch_uploads d
                                     WHERE (d.firm_id, d.return_id) = (r.firm_id, r.return_id)
                                       AND d.job_id IS NULL AND d.state NOT IN ('misassigned', 'unassigned'))""",
                             (principal.firm_id, return_id, revision))
        return ReviewDecisionRecorded(decision_version=version)


def create_firm_app(
    *, authenticate: Authenticate, db: CoordinatorDB, tax_store: PostgresTaxStore, intake: BatchIntake,
    objects: ImmutableObjects, ownership: OwnershipStore, extraction_policy: ExtractionPolicy | None = None,
) -> FastAPI:
    """Build the service against explicitly provisioned infrastructure and a firm authenticator."""
    if extraction_policy is not None and extraction_policy.data_class not in {'synthetic', 'approved_redacted'}:
        raise ValueError('External intake requires a synthetic or approved redacted policy.')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    router = APIRouter(prefix='/api/sigma/v1')
    data = _FirmData(db, tax_store, objects, ownership)

    @app.middleware('http')
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(ReviewError)
    async def review_error(_request: Request, error: ReviewError):
        headers = {'WWW-Authenticate': 'Bearer'} if error.status_code == 401 else None
        return JSONResponse({'detail': error.detail}, status_code=error.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        return JSONResponse({'detail': 'The request does not match the firm API contract.'}, status_code=422)

    @app.exception_handler(OwnershipBlocked)
    @app.exception_handler(TaxConflict)
    async def conflict(_request: Request, _error: Exception):
        return JSONResponse({'detail': 'The exact review state or checkout no longer permits this action.'}, status_code=409)

    @app.exception_handler(TaxNotFound)
    @app.exception_handler(TaxAccessDenied)
    async def missing(_request: Request, _error: Exception):
        return JSONResponse({'detail': 'Evidence not found in this assignment and revision.'}, status_code=404)

    @app.exception_handler(DatabaseError)
    async def database_unavailable(_request: Request, _error: DatabaseError):
        return JSONResponse({'detail': 'The firm database could not complete the request.'}, status_code=503)

    def principal_for_request(
        authorization: Annotated[list[str] | None, Header()] = None,
        x_sigma_machine: Annotated[list[str] | None, Header()] = None,
        x_sigma_profile: Annotated[list[str] | None, Header()] = None,
    ) -> Principal:
        if not all(values and len(values) == 1 for values in (authorization, x_sigma_machine, x_sigma_profile)):
            raise ReviewError(401, 'An individual firm access token and enrolled device are required.')
        token = _BEARER.fullmatch(authorization[0])
        machine, profile = x_sigma_machine[0], x_sigma_profile[0]
        if token is None or not _KEY.fullmatch(machine) or not _KEY.fullmatch(profile):
            raise ReviewError(401, 'An individual firm access token and enrolled device are required.')
        try:
            principal = authenticate(token[1], machine, profile)
        except PermissionError:
            principal = None
        if (not isinstance(principal, Principal) or (principal.machine_id, principal.profile_id) != (machine, profile)
                or not _KEY.fullmatch(principal.firm_id) or not _KEY.fullmatch(principal.actor_id)):
            raise ReviewError(401, 'The firm could not authenticate this preparer and device.')
        return principal

    def scope(principal: Principal) -> IngestScope:
        return IngestScope(principal.firm_id, principal.actor_id, principal.return_ids)

    @router.get('/returns')
    def returns(principal: Principal = Depends(principal_for_request)):
        return data.returns(principal)

    @router.get('/returns/{return_id}/access')
    def access(return_id: Ref, principal: Principal = Depends(principal_for_request)):
        data.authorize(principal, return_id)
        return {'firmId': principal.firm_id, 'preparerId': principal.actor_id, 'returnId': return_id}

    @router.get('/returns/{return_id}/jobs')
    def jobs(return_id: Ref, principal: Principal = Depends(principal_for_request)):
        return data.jobs(principal, return_id)

    @router.get('/returns/{return_id}/evidence', response_model=EvidenceDetail)
    def evidence(return_id: Ref, revision: Ref | None = None, principal: Principal = Depends(principal_for_request)):
        return data.evidence(principal, return_id, revision)

    @router.get('/returns/{return_id}/originals/{document_id}/{version_id}')
    def original(return_id: Ref, document_id: Ref, version_id: Ref, revision: Ref,
                 principal: Principal = Depends(principal_for_request)):
        return data.original(principal, return_id, revision, OriginalRef(document_id=document_id, version_id=version_id))

    @router.get('/intake', response_model=IntakeSettings)
    def intake_settings(_principal: Principal = Depends(principal_for_request)):
        if extraction_policy is None:
            return IntakeSettings(enabled=True, requires_client=True,
                                  message='External extraction is not enabled. Originals can still be uploaded for review.')
        classes = 'synthetic documents' if extraction_policy.data_class == 'synthetic' else 'firm-approved redacted documents'
        return IntakeSettings(enabled=True, requires_client=True,
                              approval_label=f"I authorize {extraction_policy.provider_id} to process this batch of {classes}.")

    @router.get('/intake/clients', response_model=list[IntakeClient])
    def intake_clients(principal: Principal = Depends(principal_for_request)):
        return data.intake_clients(principal)

    @router.get('/intake/batches', response_model=list[BatchView])
    def batches(principal: Principal = Depends(principal_for_request)):
        return intake.list_batches(scope(principal))

    @router.get('/intake/batches/{batch_id}', response_model=BatchView)
    def batch(batch_id: Ref, principal: Principal = Depends(principal_for_request)):
        return intake.status(scope(principal), batch_id)

    @router.get('/returns/{return_id}/intake/batches', response_model=list[BatchView])
    def return_batches(return_id: Ref, principal: Principal = Depends(principal_for_request)):
        data.authorize(principal, return_id)
        return intake.list_batches(scope(principal), return_id=return_id)

    def declare(body: BatchManifest, principal: Principal) -> BatchView:
        assignment, hint = data.resolve_assignment(principal, body.assignment, body.return_hint)
        if body.approved_for_ai and extraction_policy is None:
            raise ReviewError(409, 'No approved external extraction policy is configured.')
        # The canonical return is a routing hint. Committed receipt classification,
        # verified document attribution, and disclosure authorization remain independent.
        manifest = body.model_copy(update={'assignment': assignment, 'return_hint': hint})
        return intake.declare(scope(principal), manifest, policy=extraction_policy)

    @router.post('/intake/batches', status_code=201, response_model=BatchView)
    def declare_batch(body: BatchManifest, principal: Principal = Depends(principal_for_request)):
        return declare(body, principal)

    @router.post('/returns/{return_id}/intake/batches', status_code=201, response_model=BatchView)
    def declare_return_batch(return_id: Ref, body: BatchManifest, principal: Principal = Depends(principal_for_request)):
        data.authorize(principal, return_id)
        if body.return_hint != return_id:
            raise ReviewError(409, 'This declaration requires an explicit matching return hint.')
        return declare(body, principal)

    async def upload(request: Request, principal: Principal, batch_id: str, slot: str, return_id: str | None) -> BatchView:
        if return_id is not None:
            await run_in_threadpool(data.authorize, principal, return_id)
        media_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        if media_type not in {'application/pdf', 'text/plain'}:
            raise ReviewError(415, 'Upload a selectable-text PDF or UTF-8 text file.')
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_PDF_BYTES:
                raise ReviewError(413, 'Documents must be at most 10 MiB.')
            content.extend(chunk)
        return await run_in_threadpool(intake.put, scope(principal), batch_id, slot, media_type, bytes(content),
                                       return_id=return_id, require_assignment=True)

    @router.put('/intake/batches/{batch_id}/files/{slot}', response_model=BatchView)
    async def put_file(batch_id: Ref, slot: Ref, request: Request,
                       return_id: Annotated[Ref | None, Query(alias='returnId')] = None,
                       principal: Principal = Depends(principal_for_request)):
        return await upload(request, principal, batch_id, slot, return_id)

    @router.put('/returns/{return_id}/intake/batches/{batch_id}/files/{slot}', response_model=BatchView)
    async def put_return_file(return_id: Ref, batch_id: Ref, slot: Ref, request: Request,
                              principal: Principal = Depends(principal_for_request)):
        return await upload(request, principal, batch_id, slot, return_id)

    @router.post('/intake/batches/{batch_id}/files/{slot}/exclude', response_model=BatchView)
    def exclude(batch_id: Ref, slot: Ref, principal: Principal = Depends(principal_for_request)):
        return intake.exclude(scope(principal), batch_id, slot)

    @router.put('/intake/batches/{batch_id}/files/{slot}/assignment', response_model=BatchView)
    def assign_slot(batch_id: Ref, slot: Ref, body: ClientAssignment,
                    principal: Principal = Depends(principal_for_request)):
        data.resolve_assignment(principal, body, None)
        return intake.assign_slot(scope(principal), batch_id, slot, body)

    @router.post('/returns/{return_id}/review/reservations', response_model=_Reservation)
    def request_review(return_id: Ref, body: _ReservationRequest, principal: Principal = Depends(principal_for_request)):
        with db.transaction() as conn:
            data.target(conn, principal, return_id)
            held = data.held_review(conn, principal, return_id)
        if held is not None:
            return data.reservation(principal, return_id, review_id=held['review_id'], allow_reopen=False)
        with db.transaction() as conn:
            data.target(conn, principal, return_id)
            _tax_revision(data.select_job(data.job_rows(conn, principal, return_id), body.input_revision))
        review_id = ownership.request_review(principal.firm_id, return_id, principal.machine_id, principal.actor_id,
                                             input_revision=body.input_revision)
        return data.claim(principal, return_id, review_id, queue_only=body.queue_only)

    @router.get('/returns/{return_id}/review/reservations', response_model=_Reservation)
    def find_review(return_id: Ref, input_revision: Annotated[Ref, Query(alias='inputRevision')],
                    principal: Principal = Depends(principal_for_request)):
        return data.reservation(principal, return_id, input_revision=input_revision)

    @router.get('/returns/{return_id}/review/reservations/{review_id}', response_model=_Reservation)
    def review(return_id: Ref, review_id: Ref, principal: Principal = Depends(principal_for_request)):
        return data.reservation(principal, return_id, review_id=review_id)

    @router.post('/returns/{return_id}/review/reservations/{review_id}/claim', response_model=_Reservation)
    def claim_review(return_id: Ref, review_id: Ref, principal: Principal = Depends(principal_for_request)):
        return data.claim(principal, return_id, review_id)

    @router.post('/returns/{return_id}/review/reservations/{review_id}/activate', response_model=_Reservation)
    def activate(return_id: Ref, review_id: Ref, body: _ReservationProof, principal: Principal = Depends(principal_for_request)):
        return data.proof(principal, return_id, review_id, body, close=False)

    @router.post('/returns/{return_id}/review/reservations/{review_id}/close', response_model=_Reservation)
    def close(return_id: Ref, review_id: Ref, body: _ReservationProof, principal: Principal = Depends(principal_for_request)):
        return data.proof(principal, return_id, review_id, body, close=True)

    @router.post('/returns/{return_id}/review/reservations/{review_id}/cancel', response_model=_Reservation)
    def cancel(return_id: Ref, review_id: Ref, principal: Principal = Depends(principal_for_request)):
        with db.transaction() as conn:
            data.target(conn, principal, return_id)
            data.review_row(conn, principal, return_id, review_id=review_id)
        if not ownership.cancel_review(principal.firm_id, return_id, review_id, principal.actor_id):
            raise ReviewError(409, 'Only a queued review may be cancelled without native exit proof.')
        return data.reservation(principal, return_id, review_id=review_id)

    @router.post('/returns/{return_id}/review/decisions', response_model=ReviewDecisionRecorded)
    def decide(return_id: Ref, body: ReviewDecision, principal: Principal = Depends(principal_for_request)):
        return data.decision(principal, return_id, body)

    app.include_router(router)
    return app
