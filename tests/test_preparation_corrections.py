from dataclasses import replace
from decimal import Decimal

import pytest

from src.preparation.contracts import Amount, LineEvidence, OriginalRef, RecordIdentity
from src.preparation.corrections import BoxEvidence, CorrectionBox, PriorW2, SavedBox, plan_w2c


IDENTITY = RecordIdentity(
    tax_year=2025, form="w2", subject_reference="employee", issuer_reference="employer",
    instance_reference="instance", activity_reference=None, period=None,
)
CORRECTION = IDENTITY.model_copy(update={"form": "w2c"})


def amount(value):
    return Amount(kind="amount", value=Decimal(value))


def support(value, version, line):
    return BoxEvidence(amount(value), (LineEvidence(
        precision="lines", original=OriginalRef(document_id="w2", version_id=version),
        line_start=line, line_end=line, quote=value,
    ),))


PRIOR = PriorW2("w2-one", IDENTITY, {"box1": support("30000", "v1", 1), "box2": support("800", "v1", 2)})
CHANGE = CorrectionBox("box2", support("800", "v2", 1), support("0", "v2", 2))


@pytest.mark.parametrize(("native", "status"), [
    (SavedBox(amount("800"), False), "apply"),
    (SavedBox(amount("900"), False), "conflict"),
    (SavedBox(amount("800"), True), "conflict"),
    (SavedBox(amount("0"), True), "already_current"),
    (None, "unknown_native"),
])
def test_zero_correction_changes_only_the_explicit_box(native, status):
    saved = {"box1": SavedBox(amount("30000"), True)}
    if native is not None:
        saved["box2"] = native
    plan = plan_w2c(CORRECTION, [CHANGE], [PRIOR], {PRIOR.record_id: saved}, filed=False)
    assert plan.record_id == PRIOR.record_id
    (row,) = plan.changes
    assert (row.box, row.status) == ("box2", status)
    assert row.prior_source == PRIOR.source_boxes["box2"]
    assert row.proposal.value.value == 0
    assert row.native == native


def test_wrong_employer_ambiguous_predecessor_and_filed_return_never_plan_a_write():
    wrong_employer = CORRECTION.model_copy(update={"issuer_reference": "other-employer"})
    assert plan_w2c(wrong_employer, [CHANGE], [PRIOR], {}, filed=False).issue == "unmatched_predecessor"
    assert plan_w2c(CORRECTION, [CHANGE], [PRIOR, PRIOR], {}, filed=False).issue == "unmatched_predecessor"
    assert plan_w2c(CORRECTION, [CHANGE], [PRIOR], {}, filed=True).issue == "filed_return"


def test_previously_reported_column_must_match_prior_source():
    out_of_order = replace(CHANGE, previous=support("700", "v2", 1))
    plan = plan_w2c(CORRECTION, [out_of_order], [PRIOR], {}, filed=False)
    assert plan.changes[0].status == "unverified_correction"


def test_correction_columns_must_belong_to_the_same_new_original():
    for reported, corrected in (
        (support("800", "v2", 1), support("0", "v3", 2)),
        (support("800", "v1", 1), support("0", "v1", 2)),
    ):
        plan = plan_w2c(CORRECTION, [CorrectionBox("box2", reported, corrected)], [PRIOR], {
            PRIOR.record_id: {"box2": SavedBox(amount("800"), False)},
        }, filed=False)
        assert plan.changes[0].status == "unverified_correction"
