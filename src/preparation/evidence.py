"""Ground documentary claims against an authorized immutable original and trusted fact context."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..review_documents import DocumentError, extract_region, literal_citation, source_pages
from .contracts import (
    Candidate,
    Contract,
    DocumentEvidence,
    Evidence,
    ExactClaim,
    FactValue,
    LineEvidence,
    OriginalRef,
    PageEvidence,
    RecordIdentity,
    RegionEvidence,
    SourceClaim,
    SourceOccurrence,
    Text,
)
from .tool_contract import Ref


class OriginalScope(Contract):
    firm_id: Ref
    return_id: Ref
    original: OriginalRef


@dataclass(frozen=True)
class StoredOriginal:
    """Metadata and bytes returned by an access-checked, version-scoped repository."""

    scope: OriginalScope
    media_type: str
    digest: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class LoadedOriginal:
    original: StoredOriginal = field(repr=False)
    _pages: list[dict] = field(repr=False)

    @property
    def scope(self) -> OriginalScope:
        return self.original.scope

    def numbered_pages(self) -> list[dict]:
        """Return bounded text and one-based line references, without PDF geometry."""
        is_pdf = self.original.media_type == "application/pdf"
        return [
            {"page": page["page"] if is_pdf else None,
             "lines": [{"line": number, "text": line["text"]}
                       for number, line in enumerate(page["lines"], 1)]}
            for page in self._pages
        ]


def load_original(scope: OriginalScope, read_original: Callable[[OriginalScope], StoredOriginal]) -> LoadedOriginal:
    """Load only the authorized version. The reader must enforce the caller's grant."""
    original = read_original(scope)
    if original.scope != scope:
        raise DocumentError("Original not found in this return and document version.", 404)
    if not isinstance(original.content, bytes) or sha256(original.content).hexdigest() != original.digest:
        raise DocumentError("The original does not match its stored version.")
    return LoadedOriginal(original, source_pages(original.content, original.media_type))


VerifiedAnchor = Annotated[ExactClaim | PageEvidence, Field(discriminator="precision")]


class VerifiedFact(Contract):
    """An independent local verifier's finding, never supplied by extraction output.

    The verifier must establish the subject, year, issuer, form instance, and
    box/column or ledger row for this value. Attest exact lines and literal quote
    only when they identify the specific entry; otherwise attest a known PDF page.
    """

    scope: OriginalScope
    identity: RecordIdentity
    fact_key: Ref
    column: Literal["reported", "previously_reported", "correct_information"]
    raw_value: Text
    normalized: FactValue
    anchors: Annotated[tuple[VerifiedAnchor, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def validate_original(self):
        if any(anchor.original != self.scope.original for anchor in self.anchors):
            raise ValueError("Verified anchor is outside its original version.")
        return self


class GroundedCandidate(Contract):
    scope: OriginalScope
    identity: RecordIdentity
    candidate: Candidate
    citations: Annotated[tuple[Evidence, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def validate_original(self):
        if any(c.original != self.scope.original for c in (*self.candidate.citations, *self.citations)):
            raise ValueError("Citation is outside its original version.")
        return self

    @property
    def eligible(self) -> bool:
        """Documentary support for a value; conflict and target checks are separate."""
        return (self.candidate.normalized.kind not in {"missing", "not_applicable"}
                and any(c.precision != "document" for c in self.citations))


def _ground_claim(
    source: LoadedOriginal,
    claim: SourceClaim,
    anchors: set[ExactClaim | PageEvidence],
    document: DocumentEvidence,
) -> Evidence:
    if not anchors or claim.precision == "document":
        return document

    media_type = source.original.media_type
    page = claim.page
    page_only: PageEvidence | None = None
    fallback: DocumentEvidence | PageEvidence = document
    if media_type == "application/pdf":
        if page is None or page > len(source._pages):
            return document
        page_only = PageEvidence(precision="page", original=source.scope.original, page=page)
        if page_only in anchors:
            fallback = page_only
    elif claim.precision != "exact" or page is not None:
        return fallback

    if claim.precision != "exact" or claim not in anchors:
        return fallback
    # The old helper represents TXT internally as one page; external TXT references have no page.
    source_page = 1 if page is None else page
    try:
        grounded = literal_citation(source._pages, page=source_page, line_start=claim.line_start,
                                    line_end=claim.line_end, quote=claim.quote, media_type=media_type)
        if page_only is not None:
            fallback = page_only
        lines = source._pages[source_page - 1]["lines"][claim.line_start - 1:claim.line_end]
        literal = "\n".join(line["text"] for line in lines)
        if literal.find(claim.quote, literal.index(claim.quote) + 1) != -1:
            return fallback
        if page is None:
            return LineEvidence(precision="lines", original=source.scope.original, line_start=claim.line_start,
                                line_end=claim.line_end, quote=claim.quote)
        left, top, width, height = grounded["rect"]
        rect = (left, top, width, height)
        # A union of PDF line boxes can also include other glyphs. Do not highlight it in that case.
        if extract_region(source.original.content, page, rect) != literal:
            return fallback
    except DocumentError:
        return fallback
    return RegionEvidence(precision="region", original=source.scope.original, page=page,
                          rect=rect, quote=claim.quote)


def ground_occurrence(source: LoadedOriginal, occurrence: SourceOccurrence,
                      verified_facts: Iterable[VerifiedFact]) -> tuple[GroundedCandidate, ...]:
    """Match claims only to independently verified facts for this return and original version."""
    if occurrence.original != source.scope.original:
        raise DocumentError("Original not found in this return and document version.", 404)
    anchors_by_fact: dict[tuple[str, str, str, FactValue], set[ExactClaim | PageEvidence]] = defaultdict(set)
    for fact in verified_facts:
        if fact.scope == source.scope and fact.identity == occurrence.identity:
            anchors_by_fact[fact.fact_key, fact.column, fact.raw_value, fact.normalized].update(fact.anchors)

    fallback = DocumentEvidence(precision="document", original=source.scope.original)
    result = []
    for candidate in occurrence.candidates:
        anchors = anchors_by_fact.get((candidate.fact_key, candidate.column, candidate.raw_value, candidate.normalized), set())
        citations = tuple(dict.fromkeys(_ground_claim(source, claim, anchors, fallback) for claim in candidate.citations))
        result.append(GroundedCandidate(scope=source.scope, identity=occurrence.identity,
                                        candidate=candidate, citations=citations))
    return tuple(result)
