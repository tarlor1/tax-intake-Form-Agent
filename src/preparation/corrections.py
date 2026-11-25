"""Plan field changes for an already matched, unfiled W-2; never append a row."""

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from .contracts import Amount, Evidence, RecordIdentity


@dataclass(frozen=True, slots=True)
class BoxEvidence:
    value: Amount
    evidence: tuple[Evidence, ...]

    @property
    def located(self) -> bool:
        return bool(self.evidence) and all(anchor.precision != "document" for anchor in self.evidence)


def verified_correction_pair(baseline: BoxEvidence, reported: BoxEvidence, corrected: BoxEvidence) -> bool:
    if not (baseline.located and reported.located and corrected.located
            and reported.value.value == baseline.value.value):
        return False
    correction_originals = {anchor.original for anchor in reported.evidence}
    return (
        len(correction_originals) == 1
        and correction_originals == {anchor.original for anchor in corrected.evidence}
        and correction_originals.isdisjoint(anchor.original for anchor in baseline.evidence)
        and not any(anchor in corrected.evidence for anchor in reported.evidence)
    )


@dataclass(frozen=True, slots=True)
class PriorW2:
    record_id: str
    identity: RecordIdentity
    source_boxes: Mapping[str, BoxEvidence]


@dataclass(frozen=True, slots=True)
class CorrectionBox:
    box: str
    previous: BoxEvidence | None
    corrected: BoxEvidence | None


@dataclass(frozen=True, slots=True)
class SavedBox:
    value: Amount | None
    preparer_owned: bool


@dataclass(frozen=True, slots=True)
class BoxChange:
    box: str
    prior_source: BoxEvidence | None
    native: SavedBox | None
    proposal: BoxEvidence | None
    status: Literal["apply", "already_current", "conflict", "unverified_correction", "unknown_native"]


@dataclass(frozen=True, slots=True)
class CorrectionPlan:
    record_id: str | None
    issue: Literal["filed_return", "unmatched_predecessor", "ambiguous_correction"] | None
    changes: tuple[BoxChange, ...] = ()


def plan_w2c(
    correction: RecordIdentity,
    boxes: Sequence[CorrectionBox],
    predecessors: Sequence[PriorW2],
    saved_by_record: Mapping[str, Mapping[str, SavedBox]],
    *,
    filed: bool,
) -> CorrectionPlan:
    """Use only current, return-scoped predecessor facts and saved native observations."""
    if filed:
        return CorrectionPlan(None, "filed_return")
    if correction.form != "w2c" or not correction.issuer_reference or not boxes:
        return CorrectionPlan(None, "ambiguous_correction")
    if len({box.box for box in boxes}) != len(boxes):
        return CorrectionPlan(None, "ambiguous_correction")

    original = correction.model_copy(update={"form": "w2"})
    matches = [prior for prior in predecessors if prior.identity == original]
    if len(matches) != 1:
        return CorrectionPlan(None, "unmatched_predecessor")

    prior, = matches
    saved = saved_by_record.get(prior.record_id, {})
    changes = []
    for box in boxes:
        baseline = prior.source_boxes.get(box.box)
        native = saved.get(box.box)
        proposal = box.corrected
        reported = box.previous
        if (
            baseline is None or reported is None or proposal is None
            or not verified_correction_pair(baseline, reported, proposal)
        ):
            status = "unverified_correction"
        elif native is None or native.value is None:
            status = "unknown_native"
        elif native.value.value == proposal.value.value:
            status = "already_current"
        elif native.preparer_owned or native.value.value != baseline.value.value:
            status = "conflict"
        else:
            status = "apply"
        changes.append(BoxChange(box.box, baseline, native, proposal, status))
    return CorrectionPlan(prior.record_id, None, tuple(changes))
