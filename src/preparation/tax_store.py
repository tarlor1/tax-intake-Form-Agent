"""Revision-scoped tax evidence for the central service and isolated test fixture.

The firm service authenticates access and verifies native identities and readbacks
before calling this store. Storage of original bytes, ownership and native I/O
remain outside this persistence layer.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AwareDatetime, Field, StrictInt, TypeAdapter, model_validator

from .contracts import Amount, Contract, Evidence, ExtractionIssue, FactValue, OriginalRef, RecordIdentity, Text
from .corrections import BoxEvidence, verified_correction_pair
from .evidence import GroundedCandidate
from .tool_contract import Ref

if TYPE_CHECKING:
    from psycopg import Connection, Cursor
    from psycopg.rows import DictRow

    from .coordinator_db import CoordinatorDB


RevisionNumber = Annotated[StrictInt, Field(ge=1)]
TaxYear = Annotated[StrictInt, Field(ge=1900, le=2200)]
Scope = Literal["read", "ingest", "prepare", "review"]


class TaxAccessDenied(PermissionError):
    pass


class TaxNotFound(LookupError):
    pass


class TaxConflict(ValueError):
    pass


class TaxAccess(Contract):
    """Trusted authorization context; never deserialize this from a client request."""

    firm_id: Ref
    return_id: Ref
    actor_id: Ref
    scopes: frozenset[Scope]
    active_revision: RevisionNumber | None = None

    def require(self, scope: Scope, revision: int | None = None) -> None:
        if scope not in self.scopes or (revision is not None and self.active_revision != revision):
            raise TaxAccessDenied("This grant does not authorize that operation and revision.")

    @property
    def key(self) -> tuple[str, str]:
        return self.firm_id, self.return_id


class OriginalPointer(Contract):
    """Versioned reference and digest for bytes held outside this store."""

    original: OriginalRef
    storage_ref: Text
    digest: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
    media_type: Literal["application/pdf", "text/plain"]


class TaxRecord(Contract):
    record_id: Ref
    identity: RecordIdentity


class FactInput(Contract):
    fact_id: Ref
    record_id: Ref
    occurrence_id: Ref
    provenance: Text
    predecessor_fact_id: Ref | None = None
    grounded: GroundedCandidate


class CitedFact(Contract):
    fact_id: Ref
    record_id: Ref
    occurrence_id: Ref
    original: OriginalRef
    fact_key: Ref
    column: Literal["reported", "previously_reported", "correct_information"]
    raw_value: Text
    normalized: FactValue
    provenance: Text
    citations: Annotated[tuple[Evidence, ...], Field(min_length=1, max_length=16)]
    predecessor_fact_id: Ref | None = None

    @property
    def eligible(self) -> bool:
        return self.normalized.kind not in {"missing", "not_applicable"} and any(
            citation.precision != "document" for citation in self.citations
        )


class NativeDestination(Contract):
    """The profile ID denotes an approved version, and the key reopens the exact draft."""

    profile_id: Ref
    draft_key: Text


class SourceField(Contract):
    record_id: Ref
    fact_key: Ref


class NativeBinding(Contract):
    """Semantic native address; a missing record key denotes a singleton section only."""

    binding_id: Ref
    section: Ref
    subform: Ref | None = None
    record_key: Text | None = None
    field_key: Ref
    source_fields: tuple[SourceField, ...] = ()

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.source_fields)) != len(self.source_fields):
            raise ValueError("A binding cannot map the same source field twice.")
        return self


@dataclass(frozen=True)
class ApprovedSum:
    """Application-owned, profile-scoped rule; never construct it from an extraction."""

    rule_id: str
    input_fact_keys: tuple[str, ...]


DerivationAddress = tuple[str, str, str | None, str]  # profile, section, subform, field
_source_fields_adapter = TypeAdapter(tuple[SourceField, ...])


class NativeReading(Contract):
    value: FactValue
    observed_at: AwareDatetime


class FieldState(Contract):
    # None is unknown/unobserved; value kind "missing" denotes an observed blank.
    candidate_ids: tuple[Ref, ...] = ()
    used_candidate_ids: tuple[Ref, ...] = ()
    proposed: FactValue | None = None
    derivation: Text | None = None
    derivation_rule_id: Ref | None = None
    entered: NativeReading | None = None
    visible: NativeReading | None = None
    reopened: NativeReading | None = None
    reviewer_saved: NativeReading | None = None

    @property
    def last_saved(self) -> NativeReading | None:
        if self.reopened is None:
            return self.reviewer_saved
        if self.reviewer_saved is None or self.reopened.observed_at > self.reviewer_saved.observed_at:
            return self.reopened
        return self.reviewer_saved

    @model_validator(mode="after")
    def check_candidates(self):
        candidates = set(self.candidate_ids)
        if len(candidates) != len(self.candidate_ids) or len(set(self.used_candidate_ids)) != len(self.used_candidate_ids):
            raise ValueError("A field cannot link the same candidate twice.")
        if not candidates.issuperset(self.used_candidate_ids):
            raise ValueError("Used candidates must be among the field's linked candidates.")
        if (self.proposed is not None or self.entered is not None) and not self.used_candidate_ids:
            raise ValueError("Preparation proposals and entries require source evidence.")
        return self


class FieldUpdate(Contract):
    binding: NativeBinding
    state: FieldState


@dataclass(frozen=True)
class FieldChange:
    version: int
    binding_id: str
    previous: FieldState | None
    current: FieldState
    actor_id: str
    recorded_at: datetime


class ChooseSource(Contract):
    kind: Literal["choose_source"]
    binding_id: Ref
    candidate_id: Ref


class AcceptEdit(Contract):
    kind: Literal["accept_edit"]
    binding_id: Ref
    accepted: NativeReading


class VerifyEdit(Contract):
    kind: Literal["verify_edit"]
    binding_id: Ref
    accepted_version: RevisionNumber
    saved: NativeReading


class MarkReviewed(Contract):
    kind: Literal["mark_reviewed"]


Decision = Annotated[ChooseSource | AcceptEdit | VerifyEdit | MarkReviewed, Field(discriminator="kind")]
_decision_adapter = TypeAdapter(Decision)
_tax_year_adapter = TypeAdapter(TaxYear)


@dataclass(frozen=True)
class ReviewerDecision:
    version: int
    actor_id: str
    recorded_at: datetime
    decision: Decision
    reviewed_field_version: int | None = None
    reviewed_decision_version: int | None = None


@dataclass(frozen=True)
class RevisionView:
    revision: int
    field_version: int
    decision_version: int
    created_at: datetime
    created_by: str
    review_required_new_document: bool
    destination: NativeDestination | None
    originals: tuple[OriginalPointer, ...]
    issues: tuple[ExtractionIssue, ...]
    records: dict[str, TaxRecord]
    facts: dict[str, CitedFact]
    fields: dict[str, FieldUpdate]
    changes: tuple[FieldChange, ...]
    decisions: tuple[ReviewerDecision, ...]
    effective_facts: dict[SourceField, tuple[CitedFact, ...]]
    fact_revisions: dict[str, int]

    def candidate_ids_for_binding(self, binding_id: str) -> tuple[str, ...]:
        """Include pending source alternatives without mutating the saved field snapshot."""
        field = self.fields[binding_id]
        linked = dict.fromkeys(field.state.candidate_ids)
        for fact in self.facts.values():
            if _source_field(fact) in field.binding.source_fields:
                linked[fact.fact_id] = None
        return tuple(linked)


def _source_field(fact: CitedFact) -> SourceField:
    return SourceField(record_id=fact.record_id, fact_key=fact.fact_key)


def effective_source_facts(
    history: Sequence[tuple[int, CitedFact]],
) -> dict[SourceField, tuple[CitedFact, ...]]:
    """Resolve eligible evidence by logical record and box, without changing history."""
    groups: dict[SourceField, dict[int, list[CitedFact]]] = defaultdict(lambda: defaultdict(list))
    for revision, fact in history:
        groups[_source_field(fact)][revision].append(fact)
    result = {}
    for source, revisions in groups.items():
        current: list[CitedFact] = []
        reported_value: FactValue | None = None
        corrected = False
        for revision in sorted(revisions):
            batch = revisions[revision]
            reports = tuple(fact for fact in batch if fact.column == "reported")
            corrections = [fact for fact in batch if fact.column == "correct_information"]
            comparisons = [fact for fact in batch if fact.column == "previously_reported"]
            if reports:
                baseline = current[0].normalized if corrected else reported_value
                if baseline is None:
                    baseline = reports[0].normalized
                if any(fact.normalized != baseline for fact in reports):
                    break
                reported_value = baseline
            if not corrections and not comparisons:
                if not corrected:
                    current.extend(fact for fact in reports if fact.eligible)
                continue
            if len(corrections) != 1 or len(comparisons) != 1:
                break
            correction, = corrections
            comparison, = comparisons
            predecessor = next((fact for fact in current if fact.fact_id == correction.predecessor_fact_id), None)
            if (
                predecessor is None or comparison.occurrence_id != correction.occurrence_id
                or not isinstance(predecessor.normalized, Amount)
                or not isinstance(comparison.normalized, Amount)
                or not isinstance(correction.normalized, Amount)
                or not verified_correction_pair(
                    BoxEvidence(predecessor.normalized, predecessor.citations),
                    BoxEvidence(comparison.normalized, comparison.citations),
                    BoxEvidence(correction.normalized, correction.citations),
                )
            ):
                break
            current = [correction]
            corrected = True
        else:
            if current:
                result[source] = tuple(current)
    return result


def validate_field_sources(
    binding: NativeBinding, state: FieldState, facts: Mapping[str, CitedFact],
    effective: Mapping[SourceField, tuple[CitedFact, ...]], approved_sum: ApprovedSum | None,
) -> None:
    """Apply trusted binding and derivation policy before recording a proposal or entry."""
    if not facts.keys() >= set(state.candidate_ids):
        raise TaxNotFound("Field candidate not found in this return revision.")
    linked = tuple(facts[fact_id] for fact_id in state.candidate_ids)
    sources = set(binding.source_fields)
    if any(_source_field(fact) not in sources for fact in linked):
        raise TaxConflict("Unsupported facts: linked candidate does not match the binding's logical record and field.")
    if not state.used_candidate_ids:
        return
    used_ids = set(state.used_candidate_ids)
    used_values = []
    for source in binding.source_fields:
        matches = tuple(fact for fact in effective.get(source, ()) if fact.fact_id in used_ids)
        if not matches:
            raise TaxConflict("Unsupported facts: each mapped input needs current eligible evidence.")
        used_ids.difference_update(fact.fact_id for fact in matches)
        used_values.append(matches[0].normalized)
    if used_ids:
        raise TaxConflict("Unsupported facts: superseded, conflicting, or ineligible evidence cannot justify unattended entry.")
    values = tuple(value for value in (state.proposed, state.entered.value if state.entered else None) if value is not None)
    if state.derivation_rule_id is None and len(used_values) == 1:
        if any(value != used_values[0] for value in values):
            raise TaxConflict("Proposed or entered native value does not equal its supported source value.")
        return
    if (
        approved_sum is None or approved_sum.rule_id != state.derivation_rule_id or len(used_values) < 2
        or approved_sum.input_fact_keys != tuple(source.fact_key for source in binding.source_fields)
        or not all(isinstance(value, Amount) for value in used_values)
    ):
        raise TaxConflict("Unattended derivation requires an approved profile rule with the exact mapped inputs.")
    with localcontext() as context:
        context.prec = 48
        total = sum((value.value for value in used_values), Decimal(0))
    if any(not isinstance(value, Amount) or value.value != total for value in values):
        raise TaxConflict("Proposed or entered native value does not match its approved derivation.")


def review_snapshot_is_current(
    entry: ReviewerDecision | None, field_version: int, decision_version: int,
) -> bool:
    return (
        entry is not None and isinstance(entry.decision, MarkReviewed)
        and entry.reviewed_field_version == field_version
        and entry.reviewed_decision_version == decision_version - 1
        and entry.version == decision_version
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tax_returns (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, tax_year INTEGER NOT NULL,
    latest_revision INTEGER NOT NULL,
    PRIMARY KEY (firm_id, return_id)
);
CREATE TABLE IF NOT EXISTS tax_revisions (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, revision INTEGER NOT NULL,
    parent_field_version INTEGER NOT NULL, field_version INTEGER NOT NULL,
    decision_version INTEGER NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
    review_required_new_document INTEGER NOT NULL,
    PRIMARY KEY (firm_id, return_id, revision),
    FOREIGN KEY (firm_id, return_id) REFERENCES tax_returns
);
CREATE TABLE IF NOT EXISTS tax_originals (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
    revision INTEGER NOT NULL, storage_ref TEXT NOT NULL, media_type TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (firm_id, return_id, document_id, version_id),
    FOREIGN KEY (firm_id, return_id, revision) REFERENCES tax_revisions
);
CREATE TABLE IF NOT EXISTS tax_issues (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, document_id TEXT NOT NULL, version_id TEXT NOT NULL,
    revision INTEGER NOT NULL, code TEXT NOT NULL,
    PRIMARY KEY (firm_id, return_id, document_id, version_id, revision, code),
    FOREIGN KEY (firm_id, return_id, revision) REFERENCES tax_revisions,
    FOREIGN KEY (firm_id, return_id, document_id, version_id) REFERENCES tax_originals
);
CREATE TABLE IF NOT EXISTS tax_records (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, record_id TEXT NOT NULL,
    revision INTEGER NOT NULL, identity TEXT NOT NULL,
    PRIMARY KEY (firm_id, return_id, record_id),
    UNIQUE (firm_id, return_id, identity),
    FOREIGN KEY (firm_id, return_id, revision) REFERENCES tax_revisions
);
CREATE TABLE IF NOT EXISTS tax_occurrences (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, occurrence_id TEXT NOT NULL, record_id TEXT NOT NULL,
    document_id TEXT NOT NULL, version_id TEXT NOT NULL, identity TEXT NOT NULL,
    PRIMARY KEY (firm_id, return_id, occurrence_id),
    UNIQUE (firm_id, return_id, record_id, document_id, version_id, identity),
    FOREIGN KEY (firm_id, return_id, record_id) REFERENCES tax_records,
    FOREIGN KEY (firm_id, return_id, document_id, version_id) REFERENCES tax_originals
);
CREATE TABLE IF NOT EXISTS tax_facts (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, fact_id TEXT NOT NULL, occurrence_id TEXT NOT NULL,
    fact_key TEXT NOT NULL, source_column TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (firm_id, return_id, fact_id),
    UNIQUE (firm_id, return_id, occurrence_id, fact_key, source_column),
    FOREIGN KEY (firm_id, return_id, occurrence_id) REFERENCES tax_occurrences,
    FOREIGN KEY (firm_id, return_id, revision) REFERENCES tax_revisions
);
CREATE TABLE IF NOT EXISTS tax_destinations (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, profile_id TEXT NOT NULL, draft_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (firm_id, return_id),
    UNIQUE (firm_id, profile_id, draft_key),
    FOREIGN KEY (firm_id, return_id, revision) REFERENCES tax_revisions
);
CREATE TABLE IF NOT EXISTS tax_bindings (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, binding_id TEXT NOT NULL,
    section TEXT NOT NULL, subform TEXT NOT NULL, record_key TEXT NOT NULL, field_key TEXT NOT NULL,
    source_fields TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (firm_id, return_id, binding_id),
    UNIQUE (firm_id, return_id, section, subform, record_key, field_key),
    FOREIGN KEY (firm_id, return_id) REFERENCES tax_destinations
);
CREATE TABLE IF NOT EXISTS tax_field_changes (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, revision INTEGER NOT NULL,
    field_version INTEGER NOT NULL, binding_id TEXT NOT NULL, old_state TEXT, new_state TEXT NOT NULL,
    actor_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY (firm_id, return_id, revision, field_version, binding_id),
    FOREIGN KEY (firm_id, return_id, revision) REFERENCES tax_revisions,
    FOREIGN KEY (firm_id, return_id, binding_id) REFERENCES tax_bindings
);
CREATE TABLE IF NOT EXISTS tax_review_decisions (
    firm_id TEXT NOT NULL, return_id TEXT NOT NULL, revision INTEGER NOT NULL, decision_version INTEGER NOT NULL,
    actor_id TEXT NOT NULL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL,
    reviewed_field_version INTEGER, reviewed_decision_version INTEGER,
    PRIMARY KEY (firm_id, return_id, revision, decision_version),
    FOREIGN KEY (firm_id, return_id, revision) REFERENCES tax_revisions
);
"""

# Legacy approvals have unknown snapshot versions and remain audit-only.
_SCHEMA_ADDITIONS = (
    ("tax_bindings", "source_fields", "TEXT NOT NULL DEFAULT '[]'"),
    ("tax_review_decisions", "reviewed_field_version", "INTEGER"),
    ("tax_review_decisions", "reviewed_decision_version", "INTEGER"),
)


def install_tax_schema(connection: Connection[DictRow]) -> None:
    """Install or upgrade evidence tables inside the central schema migration transaction."""
    connection.execute(_SCHEMA)
    for table, column, definition in _SCHEMA_ADDITIONS:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")


_FIXTURE_APPLICATION_ID = 0x54585256


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expect(expected: int, current: int, label: str) -> None:
    if type(expected) is not int or expected != current:
        raise TaxConflict(f"The {label} changed or the supplied version is invalid.")


def _require_revision(revision: int) -> None:
    if type(revision) is not int or revision < 1:
        raise TaxNotFound("Tax revision not found in this return.")


class _PostgresEvidenceConnection:
    def __init__(self, connection: Connection[DictRow]):
        self._connection = connection

    def execute(self, statement: str, parameters: tuple = ()) -> Cursor[DictRow]:
        # Shared evidence SQL uses question marks only as parameter placeholders.
        return self._connection.execute(statement.replace("?", "%s"), parameters)


_Database = sqlite3.Connection | _PostgresEvidenceConnection
_Row = sqlite3.Row | dict[str, Any]


class _EvidenceStore(ABC):
    _approved_sums: Mapping[DerivationAddress, ApprovedSum]

    @abstractmethod
    def _transaction(self, access: TaxAccess, *, write: bool = False, allow_missing: bool = False) -> AbstractContextManager[_Database]:
        """Lock the scoped evidence return and own or join the backend transaction."""

    def create_return(self, access: TaxAccess, tax_year: int) -> None:
        access.require("ingest")
        tax_year = _tax_year_adapter.validate_python(tax_year)
        with self._transaction(access, write=True, allow_missing=True) as db:
            db.execute("INSERT INTO tax_returns VALUES (?, ?, ?, 0)", (*access.key, tax_year))

    def ensure_return(self, access: TaxAccess, tax_year: int) -> None:
        """Create once or verify the identity already assigned to this tax year."""
        access.require("ingest")
        tax_year = _tax_year_adapter.validate_python(tax_year)
        with self._transaction(access, write=True, allow_missing=True) as db:
            db.execute(
                """INSERT INTO tax_returns (firm_id, return_id, tax_year, latest_revision) VALUES (?, ?, ?, 0)
                   ON CONFLICT (firm_id, return_id) DO NOTHING""",
                (*access.key, tax_year),
            )
            if self._return(db, access)["tax_year"] != tax_year:
                raise TaxConflict("Evidence return is already assigned to another tax year.")

    @staticmethod
    def _return(db: _Database, access: TaxAccess) -> _Row:
        row = db.execute("SELECT * FROM tax_returns WHERE firm_id = ? AND return_id = ?", access.key).fetchone()
        if row is None:
            raise TaxNotFound("Tax return not found.")
        return row

    def head_revision(self, access: TaxAccess) -> int:
        """Read the ingest frontier under the writer lock in a shared transaction."""
        access.require("ingest")
        with self._transaction(access, write=True) as db:
            return self._return(db, access)["latest_revision"]

    @staticmethod
    def _revision(db: _Database, access: TaxAccess, revision: int) -> _Row:
        _require_revision(revision)
        row = db.execute(
            "SELECT * FROM tax_revisions WHERE firm_id = ? AND return_id = ? AND revision = ?",
            (*access.key, revision),
        ).fetchone()
        if row is None:
            raise TaxNotFound("Tax revision not found in this return.")
        return row

    @staticmethod
    def _original(db: _Database, access: TaxAccess, revision: int, original: OriginalRef) -> OriginalPointer:
        row = db.execute(
            """SELECT storage_ref, media_type, digest FROM tax_originals
               WHERE firm_id = ? AND return_id = ? AND document_id = ? AND version_id = ? AND revision <= ?""",
            (*access.key, original.document_id, original.version_id, revision),
        ).fetchone()
        if row is None:
            raise TaxNotFound("Original document version not found in this return revision.")
        return OriginalPointer(original=original, **dict(row))

    def get_original(self, access: TaxAccess, revision: int, original: OriginalRef) -> OriginalPointer:
        access.require("read", revision if access.active_revision is not None else None)
        with self._transaction(access) as db:
            self._revision(db, access, revision)
            return self._original(db, access, revision, original)

    def append_revision(
        self,
        access: TaxAccess,
        expected_revision: int,
        *,
        originals: Sequence[OriginalPointer] = (),
        records: Sequence[TaxRecord] = (),
        facts: Sequence[FactInput] = (),
        issues: Sequence[ExtractionIssue] = (),
    ) -> int:
        """Freeze the next input revision. Already registered uploads do not create successors."""
        access.require("ingest")
        with self._transaction(access, write=True) as db:
            tax_return = self._return(db, access)
            head = tax_return["latest_revision"]
            _expect(expected_revision, head, "input revision")
            if head and not originals:
                raise TaxConflict("A successor revision requires a new attributable original version.")
            parent_version = self._revision(db, access, head)["field_version"] if head else 0
            revision = head + 1
            db.execute(
                "INSERT INTO tax_revisions VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)",
                (*access.key, revision, parent_version, _now(), access.actor_id, int(bool(originals))),
            )
            for stored in originals:
                original = stored.original
                db.execute(
                    "INSERT INTO tax_originals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (*access.key, original.document_id, original.version_id, revision,
                     stored.storage_ref, stored.media_type, stored.digest),
                )
            for record in records:
                if record.identity.tax_year != tax_return["tax_year"] or record.identity.form == "w2c":
                    raise TaxConflict("A logical record must match the return year and cannot be a correction form.")
                db.execute(
                    "INSERT INTO tax_records VALUES (?, ?, ?, ?, ?)",
                    (*access.key, record.record_id, revision, record.identity.model_dump_json()),
                )
            for item in facts:
                self._insert_fact(db, access, revision, item)
            for issue in issues:
                self._original(db, access, revision, issue.original)
                db.execute(
                    "INSERT INTO tax_issues VALUES (?, ?, ?, ?, ?, ?)",
                    (*access.key, issue.original.document_id, issue.original.version_id, revision, issue.code),
                )
            db.execute(
                "UPDATE tax_returns SET latest_revision = ? WHERE firm_id = ? AND return_id = ?",
                (revision, *access.key),
            )
            return revision

    @classmethod
    def _insert_fact(cls, db: _Database, access: TaxAccess, revision: int, item: FactInput) -> None:
        grounded = item.grounded
        if (grounded.scope.firm_id, grounded.scope.return_id) != access.key:
            raise TaxAccessDenied("Evidence was grounded for a different firm or return.")
        original = grounded.scope.original
        stored = cls._original(db, access, revision, original)
        candidate = grounded.candidate
        for evidence in grounded.citations:
            if evidence.original != original:
                raise TaxAccessDenied("Evidence does not refer to its immutable source version.")
            if evidence.precision != "document" and (evidence.precision == "lines") != (stored.media_type == "text/plain"):
                raise TaxConflict("Evidence precision does not match the original media type.")
        row = db.execute(
            "SELECT identity FROM tax_records WHERE firm_id = ? AND return_id = ? AND record_id = ?",
            (*access.key, item.record_id),
        ).fetchone()
        if row is None:
            raise TaxNotFound("Source record not found in this return.")
        identity = RecordIdentity.model_validate_json(row["identity"])
        correction = grounded.identity.form == "w2c"
        if grounded.identity != identity and not (
            correction and identity.form == "w2" and grounded.identity == identity.model_copy(update={"form": "w2c"})
        ):
            raise TaxConflict("Source occurrence does not match its logical tax record.")
        if (candidate.column != "reported") != correction:
            raise TaxConflict("Source column does not match the occurrence form.")
        if item.predecessor_fact_id is not None:
            if candidate.column != "correct_information":
                raise TaxConflict("Only a correction can have a predecessor fact.")
            prior = db.execute(
                """SELECT payload FROM tax_facts WHERE firm_id = ? AND return_id = ? AND fact_id = ? AND revision < ?""",
                (*access.key, item.predecessor_fact_id, revision),
            ).fetchone()
            if prior is not None:
                predecessor = CitedFact.model_validate_json(prior["payload"])
                if (predecessor.record_id != item.record_id or predecessor.fact_key != candidate.fact_key
                        or predecessor.column == "previously_reported" or predecessor.original == original):
                    raise TaxConflict("Predecessor must identify the same box in an earlier source version.")
        fact = CitedFact(
            fact_id=item.fact_id, record_id=item.record_id, occurrence_id=item.occurrence_id,
            original=original, fact_key=candidate.fact_key, column=candidate.column,
            raw_value=candidate.raw_value, normalized=candidate.normalized,
            provenance=item.provenance, citations=grounded.citations,
            predecessor_fact_id=item.predecessor_fact_id,
        )
        occurrence = (fact.record_id, original.document_id, original.version_id, grounded.identity.model_dump_json())
        existing = db.execute(
            """SELECT record_id, document_id, version_id, identity FROM tax_occurrences
               WHERE firm_id = ? AND return_id = ? AND occurrence_id = ?""",
            (*access.key, fact.occurrence_id),
        ).fetchone()
        if existing is None:
            db.execute("INSERT INTO tax_occurrences VALUES (?, ?, ?, ?, ?, ?, ?)", (*access.key, fact.occurrence_id, *occurrence))
        elif tuple(existing[key] for key in ("record_id", "document_id", "version_id", "identity")) != occurrence:
            raise TaxConflict("Source occurrence cannot change its record, identity, or document version.")
        db.execute(
            "INSERT INTO tax_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*access.key, fact.fact_id, fact.occurrence_id, fact.fact_key, fact.column, revision, fact.model_dump_json()),
        )

    @staticmethod
    def _destination(db: _Database, access: TaxAccess, revision: int) -> NativeDestination | None:
        row = db.execute(
            """SELECT profile_id, draft_key FROM tax_destinations
               WHERE firm_id = ? AND return_id = ? AND revision <= ?""",
            (*access.key, revision),
        ).fetchone()
        return NativeDestination.model_validate(dict(row)) if row else None

    @staticmethod
    def _binding(row: _Row) -> NativeBinding:
        return NativeBinding(
            binding_id=row["binding_id"], section=row["section"], subform=row["subform"] or None,
            record_key=row["record_key"] or None, field_key=row["field_key"],
            source_fields=_source_fields_adapter.validate_json(row["source_fields"]),
        )

    @classmethod
    def _fields(
        cls, db: _Database, access: TaxAccess, revision: int,
    ) -> tuple[dict[str, FieldUpdate], tuple[FieldChange, ...]]:
        # Each child captures its parent's field version. Work against an older
        # still-active revision stays there, even if a new document is pending.
        rows = db.execute(
            """SELECT c.*, b.section, b.subform, b.record_key, b.field_key, b.source_fields FROM tax_field_changes c
               JOIN tax_bindings b ON b.firm_id = c.firm_id AND b.return_id = c.return_id AND b.binding_id = c.binding_id
               LEFT JOIN tax_revisions child ON child.firm_id = c.firm_id AND child.return_id = c.return_id
                   AND child.revision = c.revision + 1
               WHERE c.firm_id = ? AND c.return_id = ? AND c.revision <= ?
                   AND (c.revision = ? OR c.field_version <= child.parent_field_version)
               ORDER BY c.revision, c.field_version, c.binding_id""",
            (*access.key, revision, revision),
        )
        fields: dict[str, FieldUpdate] = {}
        changes = []
        for row in rows:
            binding_id = row["binding_id"]
            state = FieldState.model_validate_json(row["new_state"])
            fields[binding_id] = FieldUpdate(binding=cls._binding(row), state=state)
            if row["revision"] == revision:
                changes.append(FieldChange(
                    row["field_version"], binding_id,
                    FieldState.model_validate_json(row["old_state"]) if row["old_state"] is not None else None,
                    state, row["actor_id"], datetime.fromisoformat(row["recorded_at"]),
                ))
        return fields, tuple(changes)

    @staticmethod
    def _fact_history(db: _Database, access: TaxAccess, revision: int) -> list[tuple[int, CitedFact]]:
        rows = db.execute(
            """SELECT revision, payload FROM tax_facts WHERE firm_id = ? AND return_id = ? AND revision <= ?
               ORDER BY revision, fact_id""",
            (*access.key, revision),
        )
        return [(row["revision"], CitedFact.model_validate_json(row["payload"])) for row in rows]

    @classmethod
    def _verify_binding(cls, db: _Database, access: TaxAccess, binding: NativeBinding) -> None:
        row = db.execute(
            "SELECT * FROM tax_bindings WHERE firm_id = ? AND return_id = ? AND binding_id = ?",
            (*access.key, binding.binding_id),
        ).fetchone()
        if row is None:
            if binding.record_key is not None and len(binding.source_fields) == 1:
                source = binding.source_fields[0]
                siblings = db.execute(
                    """SELECT record_key, source_fields FROM tax_bindings
                       WHERE firm_id = ? AND return_id = ? AND section = ? AND subform = ? AND record_key <> ''""",
                    (*access.key, binding.section, binding.subform or ""),
                )
                for sibling in siblings:
                    sibling_fields = _source_fields_adapter.validate_json(sibling["source_fields"])
                    if len(sibling_fields) != 1:
                        continue
                    same_row = sibling["record_key"] == binding.record_key
                    same_record = sibling_fields[0].record_id == source.record_id
                    if same_row != same_record:
                        raise TaxConflict("A logical source record and its stable native row must map one-to-one.")
            db.execute(
                """INSERT INTO tax_bindings
                   (firm_id, return_id, binding_id, section, subform, record_key, field_key, source_fields)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (*access.key, binding.binding_id, binding.section, binding.subform or "", binding.record_key or "",
                 binding.field_key, _source_fields_adapter.dump_json(binding.source_fields).decode()),
            )
        elif cls._binding(row) != binding:
            raise TaxConflict("Native binding identity cannot change.")

    @staticmethod
    def _insert_field_change(
        db: _Database, access: TaxAccess, revision: int, version: int, binding_id: str,
        previous: FieldState | None, state: FieldState, recorded_at: str,
    ) -> None:
        db.execute(
            "INSERT INTO tax_field_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*access.key, revision, version, binding_id,
             previous.model_dump_json() if previous is not None else None,
             state.model_dump_json(), access.actor_id, recorded_at),
        )

    @staticmethod
    def _advance_field_version(db: _Database, access: TaxAccess, revision: int, version: int) -> None:
        db.execute(
            "UPDATE tax_revisions SET field_version = ? WHERE firm_id = ? AND return_id = ? AND revision = ?",
            (version, *access.key, revision),
        )

    def record_fields(
        self,
        access: TaxAccess,
        revision: int,
        expected_field_version: int,
        destination: NativeDestination,
        updates: Sequence[FieldUpdate] = (),
    ) -> int:
        """Persist a verified destination and an atomic set of native field changes."""
        access.require("prepare", revision)
        with self._transaction(access, write=True) as db:
            version = self._revision(db, access, revision)["field_version"]
            _expect(expected_field_version, version, "field version")
            existing = self._destination(db, access, revision)
            if existing is None:
                db.execute(
                    "INSERT INTO tax_destinations VALUES (?, ?, ?, ?, ?)",
                    (*access.key, destination.profile_id, destination.draft_key, revision),
                )
            elif existing != destination:
                raise TaxConflict("Return already has a different verified native destination.")
            if not updates:
                return version
            fields, _ = self._fields(db, access, revision)
            history = self._fact_history(db, access, revision)
            facts = {fact.fact_id: fact for _, fact in history}
            effective = effective_source_facts(history)
            seen = set()
            changed = False
            now = _now()
            for update in updates:
                binding, state = update.binding, update.state
                if binding.binding_id in seen:
                    raise TaxConflict("A change set may address each binding only once.")
                seen.add(binding.binding_id)
                derivation = self._approved_sums.get((destination.profile_id, binding.section, binding.subform, binding.field_key))
                validate_field_sources(binding, state, facts, effective, derivation)
                self._verify_binding(db, access, binding)
                old = fields.get(binding.binding_id)
                previous = old.state if old is not None else None
                reviewer_saved = previous.reviewer_saved if previous is not None else None
                if state.reviewer_saved is not None and state.reviewer_saved != reviewer_saved:
                    raise TaxConflict("Only a verified reviewer decision can establish a reviewer-saved value.")
                if state.reviewer_saved != reviewer_saved:
                    state = state.model_copy(update={"reviewer_saved": reviewer_saved})
                if state.reopened is None and previous is not None:
                    state = state.model_copy(update={"reopened": previous.reopened})
                if previous == state:
                    continue
                changed = True
                self._insert_field_change(db, access, revision, version + 1, binding.binding_id, previous, state, now)
            if not changed:
                return version
            self._advance_field_version(db, access, revision, version + 1)
            return version + 1

    @staticmethod
    def _decisions(db: _Database, access: TaxAccess, revision: int) -> tuple[ReviewerDecision, ...]:
        rows = db.execute(
            """SELECT decision_version, actor_id, recorded_at, payload, reviewed_field_version, reviewed_decision_version
               FROM tax_review_decisions
               WHERE firm_id = ? AND return_id = ? AND revision = ? ORDER BY decision_version""",
            (*access.key, revision),
        )
        return tuple(ReviewerDecision(
            row["decision_version"], row["actor_id"], datetime.fromisoformat(row["recorded_at"]),
            _decision_adapter.validate_json(row["payload"]),
            row["reviewed_field_version"], row["reviewed_decision_version"],
        ) for row in rows)

    def record_decision(
        self,
        access: TaxAccess,
        revision: int,
        expected_field_version: int,
        expected_decision_version: int,
        decision: Decision,
    ) -> int:
        access.require("review", revision)
        with self._transaction(access, write=True) as db:
            row = self._revision(db, access, revision)
            version = row["decision_version"]
            _expect(expected_decision_version, version, "decision version")
            _expect(expected_field_version, row["field_version"], "field version")
            now = _now()
            if not isinstance(decision, MarkReviewed):
                fields, _ = self._fields(db, access, revision)
                field = fields.get(decision.binding_id)
                if field is None:
                    raise TaxNotFound("Native binding not found in this return revision.")
                if isinstance(decision, ChooseSource):
                    candidate_row = db.execute(
                        """SELECT payload FROM tax_facts
                           WHERE firm_id = ? AND return_id = ? AND fact_id = ? AND revision <= ?""",
                        (*access.key, decision.candidate_id, revision),
                    ).fetchone()
                    if candidate_row is None or _source_field(
                        CitedFact.model_validate_json(candidate_row["payload"])
                    ) not in field.binding.source_fields:
                        raise TaxNotFound("Source candidate is not linked to this binding in the revision.")
                if isinstance(decision, VerifyEdit):
                    self._verify_edit(db, access, revision, decision)
                    state = field.state.model_copy(update={"reviewer_saved": decision.saved})
                    self._insert_field_change(
                        db, access, revision, row["field_version"] + 1, decision.binding_id, field.state, state, now,
                    )
                    self._advance_field_version(db, access, revision, row["field_version"] + 1)
            signoff = isinstance(decision, MarkReviewed)
            db.execute(
                """INSERT INTO tax_review_decisions
                   (firm_id, return_id, revision, decision_version, actor_id, recorded_at, payload,
                    reviewed_field_version, reviewed_decision_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*access.key, revision, version + 1, access.actor_id, now, _decision_adapter.dump_json(decision).decode(),
                 expected_field_version if signoff else None, expected_decision_version if signoff else None),
            )
            db.execute(
                "UPDATE tax_revisions SET decision_version = ? WHERE firm_id = ? AND return_id = ? AND revision = ?",
                (version + 1, *access.key, revision),
            )
            return version + 1

    @classmethod
    def _verify_edit(cls, db: _Database, access: TaxAccess, revision: int, verified: VerifyEdit) -> None:
        for prior in reversed(cls._decisions(db, access, revision)):
            decision = prior.decision
            if not isinstance(decision, (AcceptEdit, VerifyEdit)) or decision.binding_id != verified.binding_id:
                continue
            if (isinstance(decision, AcceptEdit) and prior.version == verified.accepted_version
                    and prior.actor_id == access.actor_id and decision.accepted.value == verified.saved.value
                    and decision.accepted.observed_at <= verified.saved.observed_at):
                return
            break
        raise TaxConflict("Saved value does not verify the latest attributable, unverified native edit.")

    def read_revision(self, access: TaxAccess, revision: int | None = None) -> RevisionView:
        access.require("read")
        if access.active_revision is not None:
            revision = access.active_revision if revision is None else revision
            access.require("read", revision)
        with self._transaction(access) as db:
            if revision is None:
                revision = self._return(db, access)["latest_revision"]
            row = self._revision(db, access, revision)
            key = (*access.key, revision)
            originals = db.execute(
                """SELECT document_id, version_id, storage_ref, media_type, digest FROM tax_originals
                   WHERE firm_id = ? AND return_id = ? AND revision <= ? ORDER BY revision, document_id, version_id""", key,
            )
            issues = db.execute(
                """SELECT document_id, version_id, code FROM tax_issues
                   WHERE firm_id = ? AND return_id = ? AND revision <= ? ORDER BY revision, document_id, version_id, code""", key,
            )
            records = db.execute(
                "SELECT record_id, identity FROM tax_records WHERE firm_id = ? AND return_id = ? AND revision <= ?", key,
            )
            fields, changes = self._fields(db, access, revision)
            decisions = self._decisions(db, access, revision)
            history = self._fact_history(db, access, revision)
            signed_off = review_snapshot_is_current(decisions[-1] if decisions else None, row["field_version"], row["decision_version"])
            return RevisionView(
                revision=revision, field_version=row["field_version"], decision_version=row["decision_version"],
                created_at=datetime.fromisoformat(row["created_at"]), created_by=row["created_by"],
                review_required_new_document=bool(row["review_required_new_document"]) and not signed_off,
                destination=self._destination(db, access, revision),
                originals=tuple(OriginalPointer(
                    original=OriginalRef(document_id=item["document_id"], version_id=item["version_id"]),
                    storage_ref=item["storage_ref"], media_type=item["media_type"], digest=item["digest"],
                ) for item in originals),
                issues=tuple(ExtractionIssue(
                    original=OriginalRef(document_id=item["document_id"], version_id=item["version_id"]), code=item["code"],
                ) for item in issues),
                records={item["record_id"]: TaxRecord(
                    record_id=item["record_id"], identity=RecordIdentity.model_validate_json(item["identity"]),
                ) for item in records},
                facts={fact.fact_id: fact for _, fact in history}, fields=fields, changes=changes,
                decisions=decisions,
                effective_facts=effective_source_facts(history),
                fact_revisions={fact.fact_id: fact_revision for fact_revision, fact in history},
            )


class TaxStore(_EvidenceStore):
    """SQLite transactional fixture for an isolated local test database path."""

    def __init__(self, database_path: Path, *, approved_sums: Mapping[DerivationAddress, ApprovedSum] | None = None):
        self._approved_sums = dict(approved_sums or {})
        self._path = Path(database_path).resolve()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            if application_id == _FIXTURE_APPLICATION_ID:
                for table, column, definition in _SCHEMA_ADDITIONS:
                    if column not in {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                connection.commit()
                return
            if application_id or connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                raise TaxConflict("TaxStore requires a dedicated SQLite fixture database.")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA application_id = {_FIXTURE_APPLICATION_ID}")
            connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, access: TaxAccess, *, write: bool = False, allow_missing: bool = False) -> Iterator[_Database]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise TaxConflict("An immutable tax identity or source occurrence already exists.") from None
            except BaseException:
                connection.rollback()
                raise


class PostgresTaxStore(_EvidenceStore):
    """Shared evidence store using the coordinator's provisioned PostgreSQL schema."""

    def __init__(self, coordinator: CoordinatorDB, *, approved_sums: Mapping[DerivationAddress, ApprovedSum] | None = None):
        self._coordinator = coordinator
        self._approved_sums = dict(approved_sums or {})
        self._bound_connection: Connection[DictRow] | None = None
        self._borrowed = False

    @contextmanager
    def using_connection(self, connection: Connection[DictRow]) -> Iterator[PostgresTaxStore]:
        """Join the caller's active transaction; no independent commit or connection."""
        from psycopg.pq import TransactionStatus

        if self._borrowed or connection.info.transaction_status != TransactionStatus.INTRANS:
            raise ValueError("Evidence operations require a caller-owned active transaction.")
        bound = PostgresTaxStore(self._coordinator, approved_sums=self._approved_sums)
        bound._borrowed = True
        bound._bound_connection = connection
        try:
            yield bound
        finally:
            bound._bound_connection = None

    @contextmanager
    def _connection_transaction(self) -> Iterator[Connection[DictRow]]:
        from psycopg.pq import TransactionStatus

        if not self._borrowed:
            with self._coordinator.transaction() as connection:
                yield connection
            return
        connection = self._bound_connection
        if connection is None or connection.info.transaction_status != TransactionStatus.INTRANS:
            raise ValueError("The borrowed evidence transaction is no longer active.")
        with connection.transaction():
            yield connection

    @contextmanager
    def _transaction(self, access: TaxAccess, *, write: bool = False, allow_missing: bool = False) -> Iterator[_Database]:
        from psycopg import IntegrityError

        try:
            with self._connection_transaction() as connection:
                # Shared readers see one projection at READ COMMITTED. Other returns remain independent.
                lock = "FOR UPDATE" if write else "FOR SHARE"
                locked = connection.execute(
                    f"SELECT 1 FROM tax_returns WHERE firm_id = %s AND return_id = %s {lock}", access.key,
                ).fetchone()
                if locked is None and not allow_missing:
                    raise TaxNotFound("Tax return not found.")
                yield _PostgresEvidenceConnection(connection)
        except IntegrityError:
            raise TaxConflict("An immutable tax identity or source occurrence already exists.") from None
