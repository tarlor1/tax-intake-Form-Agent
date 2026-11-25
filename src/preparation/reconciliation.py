"""Combine corroborating evidence for one already scoped monetary question."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .corrections import BoxEvidence


@dataclass(frozen=True, slots=True)
class AmountCandidate:
    fact_id: str
    evidence: BoxEvidence


@dataclass(frozen=True, slots=True)
class SupportedTotal:
    amount: Decimal
    fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Reconciliation:
    total: SupportedTotal | None
    issue: str | None = None


def reconcile_amount(
    candidates: Sequence[AmountCandidate], *, same_coverage_verified: bool,
) -> Reconciliation:
    """Only corroborate one amount. Never sum potentially overlapping reports."""
    if not candidates:
        return Reconciliation(None, "amount-not-documented")

    fact_ids = tuple(candidate.fact_id for candidate in candidates)
    if len(set(fact_ids)) != len(fact_ids):
        raise ValueError("An amount candidate cannot occur twice in an assignment.")
    if any(not candidate.evidence.located for candidate in candidates):
        return Reconciliation(None, "unlocated-source")

    amount = candidates[0].evidence.value.value
    if len(candidates) > 1 and (
        not same_coverage_verified or any(candidate.evidence.value.value != amount for candidate in candidates)
    ):
        return Reconciliation(None, "amount-overlap-unresolved")
    return Reconciliation(SupportedTotal(amount, fact_ids))
