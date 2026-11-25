"""Ground extraction outputs, then commit the scoped artifact under the attempt."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from hashlib import sha256
import json

from .batches import CommittedBatchSource
from .contracts import ExtractionIssue, OriginalRef, TaxExtraction
from .dispatch import ExtractionTicket, PreparedExtraction
from .evidence import LoadedOriginal, OriginalScope, StoredOriginal, VerifiedFact, ground_occurrence, load_original
from .tax_store import FactInput, OriginalPointer, PostgresTaxStore, SourceField, TaxAccess, TaxRecord


DocumentVerifier = Callable[[LoadedOriginal], Iterable[VerifiedFact]]


def _id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{sha256(json.dumps(parts).encode()).hexdigest()}"


class VerifiedArtifacts:
    def __init__(self, source: CommittedBatchSource, store: PostgresTaxStore, verify: DocumentVerifier):
        self._source, self._store, self._verify = source, store, verify

    def prepare(self, ticket: ExtractionTicket, extraction: TaxExtraction) -> PreparedExtraction:
        return_id = ticket.return_id
        if return_id is None:
            return PreparedExtraction(None, None)

        original = OriginalRef(document_id=ticket.event.upload_id, version_id=ticket.event.version_id)
        scope = OriginalScope(firm_id=ticket.event.firm_id, return_id=return_id, original=original)
        media_type, content = self._source.read_extraction(ticket)
        document = load_original(scope, lambda _: StoredOriginal(scope, media_type, ticket.sha256, content))
        verified = tuple(fact for fact in self._verify(document) if fact.scope == scope)
        recognized = {fact.identity for fact in verified}
        issues = {issue.code for issue in extraction.issues if issue.original == original}
        occurrences = Counter(occurrence.identity for occurrence in extraction.records)
        grounded = []
        for occurrence in extraction.records:
            if occurrence.original != original or occurrence.identity not in recognized or occurrences[occurrence.identity] != 1:
                issues.add("unverified_identity")
                continue
            grounded.extend(ground_occurrence(document, occurrence, verified))
        if not grounded:
            issues.add("unverified_box")

        pointer = OriginalPointer(original=original, storage_ref=ticket.object_id, digest=ticket.sha256,
                                  media_type=media_type)
        access = TaxAccess(firm_id=scope.firm_id, return_id=scope.return_id, actor_id="upload-processor",
                           scopes=frozenset({"read", "ingest"}))

        def persist(connection) -> str:
            target = connection.execute("""
                SELECT tax_year, client_reference
                FROM prep_returns JOIN prep_return_directory USING (firm_id,return_id)
                WHERE (firm_id,return_id)=(%s,%s)
            """, access.key).fetchone()
            if target is None:
                raise ValueError("Verified assignment is no longer enrolled.")

            with self._store.using_connection(connection) as store:
                store.ensure_return(access, target["tax_year"])
                head = store.head_revision(access)
                prior = store.read_revision(access, head) if head else None
                records = {record.identity: record for record in prior.records.values()} if prior else {}
                new_records = []
                facts = []
                for candidate in grounded:
                    identity = candidate.identity
                    if (identity.tax_year, identity.subject_reference) != (target["tax_year"], target["client_reference"]):
                        issues.add("unverified_identity")
                        continue
                    logical = identity.model_copy(update={"form": "w2"}) if identity.form == "w2c" else identity
                    record = records.get(logical)
                    if record is None and identity.form == "w2c":
                        # A correction cannot create a second logical form or an original baseline.
                        issues.add("unverified_identity")
                        continue
                    if record is None:
                        record = TaxRecord(record_id=_id("record", logical.model_dump_json()), identity=logical)
                        records[logical] = record
                        new_records.append(record)

                    claim = candidate.candidate
                    occurrence_id = _id("occurrence", original.document_id, original.version_id, identity.model_dump_json())
                    predecessor = None
                    if prior and claim.column == "correct_information":
                        field = SourceField(record_id=record.record_id, fact_key=claim.fact_key)
                        previous = {item.candidate.normalized for item in grounded if item.identity == identity
                                    and item.candidate.fact_key == claim.fact_key
                                    and item.candidate.column == "previously_reported" and item.eligible}
                        matching = [fact.fact_id for fact in prior.effective_facts.get(field, ()) if fact.normalized in previous]
                        if len(matching) == 1:
                            predecessor = matching[0]
                    if not candidate.eligible:
                        issues.add("unverified_box")
                    facts.append(FactInput(
                        fact_id=_id("fact", occurrence_id, claim.fact_key, claim.column), record_id=record.record_id,
                        occurrence_id=occurrence_id, provenance="document-extraction", grounded=candidate,
                        predecessor_fact_id=predecessor,
                    ))
                return str(store.append_revision(access, head, originals=(pointer,), records=new_records, facts=facts,
                                                 issues=tuple(ExtractionIssue(original=original, code=code)
                                                              for code in sorted(issues))))

        return PreparedExtraction(return_id, persist)
