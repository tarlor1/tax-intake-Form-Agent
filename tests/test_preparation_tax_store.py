import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from threading import Barrier

import pytest

from src.preparation.contracts import (
    Amount,
    BooleanValue,
    Candidate,
    DocumentEvidence,
    ExactClaim,
    ExtractionIssue,
    LineEvidence,
    MissingValue,
    OriginalRef,
    PageEvidence,
    RecordIdentity,
    RegionEvidence,
)
from src.preparation.evidence import GroundedCandidate, OriginalScope
from src.preparation.tax_store import (
    AcceptEdit,
    ApprovedSum,
    ChooseSource,
    FactInput,
    FieldState,
    FieldUpdate,
    MarkReviewed,
    NativeBinding,
    NativeDestination,
    NativeReading,
    OriginalPointer,
    PostgresTaxStore,
    SourceField,
    TaxAccess,
    TaxAccessDenied,
    TaxConflict,
    TaxNotFound,
    TaxRecord,
    TaxStore,
    VerifyEdit,
    install_tax_schema,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
DESTINATION = NativeDestination(profile_id="practice:2025:v1", draft_key="draft-a")


def grant(scope="read", revision=None, *, firm_id="firm-a", return_id="return-a", actor_id=None):
    return TaxAccess(firm_id=firm_id, return_id=return_id, actor_id=actor_id or scope,
                     scopes={scope}, active_revision=revision)


def original(document_id="w2-a", version_id="v1", *, media_type="text/plain", content=b"synthetic"):
    return OriginalPointer(original=OriginalRef(document_id=document_id, version_id=version_id),
                           storage_ref=f"test:{document_id}:{version_id}", media_type=media_type,
                           digest=sha256(content).hexdigest())


def record(record_id="record-a", *, issuer="employer-a", form="w2"):
    return TaxRecord(record_id=record_id, identity=RecordIdentity(
        tax_year=2025, form=form, subject_reference="employee-a", issuer_reference=issuer,
        activity_reference=None, period=None, instance_reference="instance-a",
    ))


def amount(value):
    return Amount(kind="amount", value=value)


def fact(fact_id, tax_record, pointer, key, value, *, column="reported", evidence=None,
         scope=None, identity=None, raw="Box value: $1,234.50", predecessor=None):
    ref = pointer.original
    if evidence is None:
        evidence = LineEvidence(precision="lines", original=ref, line_start=3, line_end=4, quote=raw)
    if isinstance(evidence, (RegionEvidence, LineEvidence)):
        claim = ExactClaim(precision="exact", original=ref, page=getattr(evidence, "page", None),
                           line_start=3, line_end=4, quote=evidence.quote)
    else:
        claim = evidence
    return FactInput(
        fact_id=fact_id, record_id=tax_record.record_id,
        occurrence_id=f"{ref.document_id}-{ref.version_id}-{tax_record.record_id}", provenance="verified-fixture",
        predecessor_fact_id=predecessor,
        grounded=GroundedCandidate(
            scope=scope or OriginalScope(firm_id="firm-a", return_id="return-a", original=ref),
            identity=identity or tax_record.identity,
            candidate=Candidate(fact_key=key, column=column, raw_value=raw, normalized=value, citations=(claim,)),
            citations=(evidence,),
        ),
    )


def binding(binding_id, field_key, record_key=None, *, record_id=None):
    sources = (SourceField(record_id=record_id, fact_key=field_key),) if record_id else ()
    return NativeBinding(binding_id=binding_id, section="income", subform="wages", field_key=field_key,
                         record_key=record_key, source_fields=sources)


def reading(value, seconds=0):
    return NativeReading(value=value, observed_at=NOW + timedelta(seconds=seconds))


def update(native_binding, value, fact_id, *, candidates=None):
    return FieldUpdate(binding=native_binding, state=FieldState(
        candidate_ids=candidates or (fact_id,), used_candidate_ids=(fact_id,), proposed=value,
        entered=reading(value), visible=reading(value, 1), reopened=reading(value, 2),
    ))


@pytest.fixture
def store(tmp_path):
    fixture = TaxStore(tmp_path / "tax-fixture.sqlite3")
    fixture.create_return(grant("ingest"), 2025)
    return fixture


def test_refuses_an_unrelated_sqlite_workspace(tmp_path):
    path = tmp_path / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        with pytest.raises(TaxConflict, match="dedicated SQLite fixture"):
            TaxStore(path)
        assert db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == [("unrelated",)]


def test_correction_preserves_initial_snapshot_native_row_and_other_fields_across_reload(tmp_path):
    path = tmp_path / "tax-fixture.sqlite3"
    store = TaxStore(path)
    ingest = grant("ingest")
    read = grant()
    store.create_return(ingest, 2025)
    w2_a, w2_b = original(), original("w2-b")
    a, b = record(), record("record-b", issuer="employer-b")
    wages_a = fact("a-wages", a, w2_a, "box-1", amount("1234.50"))
    withholding_a = fact("a-tax", a, w2_a, "box-2", amount("160.20"), raw="Box 2: $160.20")
    wages_b = fact("b-wages", b, w2_b, "box-1", amount("1234.50"))
    assert store.append_revision(ingest, 0, originals=(w2_a, w2_b), records=(a, b),
                                 facts=(wages_a, withholding_a, wages_b)) == 1
    first_wages = update(binding("wages-a", "box-1", "native-row-a", record_id=a.record_id), amount("1234.50"), "a-wages")
    first_tax = update(binding("tax-a", "box-2", "native-row-a", record_id=a.record_id), amount("160.20"), "a-tax")
    second_wages = update(binding("wages-b", "box-1", "native-row-b", record_id=b.record_id), amount("1234.50"), "b-wages")
    client_answer = FieldUpdate(binding=binding("client-answer", "client-answer"), state=FieldState(
        reopened=reading(BooleanValue(kind="boolean", value=False)),
    ))
    assert store.record_fields(grant("prepare", 1), 1, 0, DESTINATION,
                               (first_wages, first_tax, second_wages, client_answer)) == 1
    assert store.record_decision(grant("review", 1), 1, 1, 0, MarkReviewed(kind="mark_reviewed")) == 1

    w2c = original("w2-a", "v2", content=b"correction")
    corrected = a.identity.model_copy(update={"form": "w2c"})
    previously = fact("a-prev-tax", a, w2c, "box-2", amount("160.20"), column="previously_reported",
                      identity=corrected, raw="Previously reported Box 2: $160.20")
    new = fact("a-correct-tax", a, w2c, "box-2", amount("0"), column="correct_information",
               identity=corrected, raw="Correct information Box 2: $0", predecessor="a-tax")
    assert store.append_revision(ingest, 1, originals=(w2c,), facts=(previously, new)) == 2
    next_tax = update(first_tax.binding, amount("0"), "a-correct-tax",
                      candidates=("a-tax", "a-prev-tax", "a-correct-tax"))
    assert store.record_fields(grant("prepare", 2), 2, 0, DESTINATION, (next_tax,)) == 1

    reopened = TaxStore(path)
    before = reopened.read_revision(read, 1)
    after = reopened.read_revision(read)
    assert before.revision == 1 and after.revision == 2
    assert set(before.records) == set(after.records) == {a.record_id, b.record_id}
    assert before.originals == (w2_a, w2_b)
    assert set(after.originals) == {w2_a, w2_b, w2c}
    assert before.facts["a-tax"].raw_value == "Box 2: $160.20"
    assert before.facts["a-tax"].citations[0].original == w2_a.original
    assert "a-correct-tax" not in before.facts
    assert after.facts["a-prev-tax"].column == "previously_reported"
    assert after.facts["a-correct-tax"].citations[0].quote == "Correct information Box 2: $0"
    assert after.facts["a-correct-tax"].citations[0].original == w2c.original
    assert after.facts["a-correct-tax"].normalized.value == Decimal(0)
    assert after.facts["a-correct-tax"].predecessor_fact_id == "a-tax"
    assert before.fields["tax-a"] == first_tax
    assert after.fields == {"tax-a": next_tax, "wages-a": first_wages, "wages-b": second_wages,
                            "client-answer": client_answer}
    assert all(change.previous is None for change in before.changes)
    change, = after.changes
    assert change.previous == first_tax.state and change.current == next_tax.state
    assert before.decisions[0].decision.kind == "mark_reviewed"
    assert not before.review_required_new_document
    assert after.review_required_new_document and not after.decisions
    assert before.destination == after.destination == DESTINATION
    assert reopened.get_original(read, 2, w2c.original) == w2c
    with pytest.raises(TaxNotFound):
        reopened.get_original(read, 1, w2c.original)
    with pytest.raises(TaxConflict):
        reopened.append_revision(ingest, 2, originals=(w2c,), facts=(new,))
    assert reopened.read_revision(read).revision == 2


@pytest.mark.parametrize("predecessor,previously_reported", [(None, "2"), ("unseen", "2"), ("original-tax", "1")])
def test_unproved_corrections_remain_candidates_without_eligible_entry(store, predecessor, previously_reported):
    ingest = grant("ingest")
    w2, w2c = original(), original("w2-a", "v2")
    a = record()
    corrected = a.identity.model_copy(update={"form": "w2c"})
    reported = fact("original-tax", a, w2, "box-2", amount("2"))
    old = fact("c-comparison", a, w2c, "box-2", amount(previously_reported),
               column="previously_reported", identity=corrected)
    new = fact("c-new", a, w2c, "box-2", amount("0"), column="correct_information",
               identity=corrected, predecessor=predecessor)
    store.append_revision(ingest, 0, originals=(w2,), records=(a,), facts=(reported,))
    store.append_revision(ingest, 1, originals=(w2c,), facts=(old, new))
    prepare = grant("prepare", 2)
    addr = binding("tax-a", "box-2", "row-a", record_id=a.record_id)
    with pytest.raises(TaxConflict, match="Unsupported facts"):
        store.record_fields(prepare, 2, 0, DESTINATION, (update(addr, amount("0"), "c-new"),))
    store.record_fields(prepare, 2, 0, DESTINATION, (FieldUpdate(binding=addr, state=FieldState(
        candidate_ids=("original-tax", "c-comparison", "c-new"),
    )),))
    view = store.read_revision(grant())
    assert len(view.facts) == 3
    assert view.facts["c-new"].predecessor_fact_id == predecessor
    assert not view.fields[addr.binding_id].state.used_candidate_ids
    choose = ChooseSource(kind="choose_source", binding_id=addr.binding_id, candidate_id="original-tax")
    assert store.record_decision(grant("review", 2), 2, 1, 0, choose) == 1
    assert store.read_revision(grant()).decisions[-1].decision == choose


def test_correction_cannot_reference_another_employer_or_box(store):
    ingest = grant("ingest")
    w2_a, w2_b, w2c = original(), original("w2-b"), original("w2-a", "v2")
    a, b = record(), record("record-b", issuer="employer-b")
    a_wages = fact("a-wages", a, w2_a, "box-1", amount("2"))
    b_tax = fact("b-tax", b, w2_b, "box-2", amount("2"))
    store.append_revision(ingest, 0, originals=(w2_a, w2_b), records=(a, b), facts=(a_wages, b_tax))
    wrong_employer = fact(
        "correction", a, w2c, "box-2", amount("0"), column="correct_information",
        identity=b.identity.model_copy(update={"form": "w2c"}),
    )
    with pytest.raises(TaxConflict, match="does not match its logical tax record"):
        store.append_revision(ingest, 1, originals=(w2c,), facts=(wrong_employer,))
    for unrelated in (a_wages, b_tax):
        correction = fact(
            "correction", a, w2c, "box-2", amount("0"), column="correct_information",
            identity=a.identity.model_copy(update={"form": "w2c"}), predecessor=unrelated.fact_id,
        )
        with pytest.raises(TaxConflict, match="same box"):
            store.append_revision(ingest, 1, originals=(w2c,), facts=(correction,))
    assert store.read_revision(grant()).revision == 1


def test_preserves_evidence_precision_and_does_not_use_document_only_candidates(store):
    text, pdf = original(), original("w2-pdf", media_type="application/pdf")
    a = record()
    region = RegionEvidence(precision="region", original=pdf.original, page=3,
                            rect=(0.1, 0.2, 0.3, 0.4), quote="Box 2: $123.40")
    page = PageEvidence(precision="page", original=pdf.original, page=4)
    document = DocumentEvidence(precision="document", original=pdf.original)
    issue = ExtractionIssue(original=pdf.original, code="unverified_box")
    facts = (
        fact("lines", a, text, "box-1", amount("1200")),
        fact("region", a, pdf, "box-2", amount("123.40"), evidence=region),
        fact("page", a, pdf, "box-3", BooleanValue(kind="boolean", value=False), evidence=page),
        fact("document", a, pdf, "box-4", amount("0"), evidence=document),
        fact("na", a, text, "box-5", MissingValue(kind="not_applicable")),
    )
    store.append_revision(grant("ingest"), 0, originals=(text, pdf), records=(a,), facts=facts, issues=(issue,))
    prepare = grant("prepare", 1)
    addr = binding("box-4", "box-4", "row-a", record_id=a.record_id)
    with pytest.raises(TaxConflict, match="Unsupported facts"):
        store.record_fields(prepare, 1, 0, DESTINATION, (update(addr, amount("0"), "document"),))
    assert store.read_revision(grant()).destination is None
    with pytest.raises(TaxConflict, match="Unsupported facts"):
        store.record_fields(prepare, 1, 0, DESTINATION, (update(addr, amount("0"), "na"),))
    observed_blank = FieldUpdate(binding=addr, state=FieldState(
        candidate_ids=("document",), visible=reading(MissingValue(kind="missing")),
    ))
    observed_unknown = FieldUpdate(binding=binding("unobserved", "box-5", "row-a", record_id=a.record_id), state=FieldState(
        candidate_ids=("na",),
    ))
    store.record_fields(prepare, 1, 0, DESTINATION, (observed_blank, observed_unknown))
    view = store.read_revision(grant())
    assert view.issues == (issue,)
    assert view.facts["region"].citations == (region,)
    assert view.facts["lines"].citations[0].line_end == 4
    assert view.facts["page"].citations == (page,)
    assert view.facts["document"].citations == (document,)
    assert view.facts["page"].normalized.value is False
    assert observed_blank.state.visible.value.kind == "missing"
    assert observed_blank.state.reopened is None and observed_unknown.state.visible is None


def test_scope_versions_and_batch_rollback(store):
    ingest = grant("ingest")
    read = grant()
    doc, a = original(), record()
    local = fact("local", a, doc, "box-1", amount("1234.50"))
    cross = local.model_copy(update={"grounded": local.grounded.model_copy(update={
        "scope": local.grounded.scope.model_copy(update={"firm_id": "firm-b"}),
    })})
    with pytest.raises(TaxAccessDenied):
        store.append_revision(ingest, 0, originals=(doc,), records=(a,), facts=(cross,))
    store.append_revision(ingest, 0, originals=(doc,), records=(a,), facts=(local,))
    for keys in ({"firm_id": "firm-b"}, {"return_id": "return-b"}):
        store.create_return(grant("ingest", **keys), 2025)
        store.append_revision(grant("ingest", **keys), 0)
        other = grant(**keys)
        assert not store.read_revision(other).facts
        with pytest.raises(TaxNotFound):
            store.get_original(other, 1, doc.original)
        with pytest.raises(TaxNotFound):
            store.record_fields(grant("prepare", 1, **keys), 1, 0, DESTINATION,
                                (update(binding("wages", "box-1"), amount("1234.50"), "local"),))
    with pytest.raises(TaxAccessDenied):
        store.get_original(ingest, 1, doc.original)
    with pytest.raises(TaxAccessDenied):
        store.append_revision(read, 1, originals=(original("other"),))
    with pytest.raises(TaxAccessDenied):
        store.record_fields(grant("prepare", 2), 1, 0, DESTINATION)
    with pytest.raises(TaxNotFound):
        store.get_original(read, 1, OriginalRef(document_id=doc.original.document_id, version_id="wrong"))
    assert store.read_revision(read).facts["local"].citations[0].original == doc.original


def test_preserves_verification_provenance_and_pins_pending_review(store):
    ingest, read = grant("ingest"), grant()
    read_edit = grant("review", 1, actor_id="preparer-a")
    store.append_revision(ingest, 0)
    addr = binding("client-input", "client-input")
    baseline = FieldUpdate(binding=addr, state=FieldState(reopened=reading(amount("1"))))
    store.record_fields(grant("prepare", 1), 1, 0, DESTINATION, (baseline,))
    assert store.append_revision(ingest, 1, originals=(original("late-upload"),)) == 2
    accepted = AcceptEdit(kind="accept_edit", binding_id=addr.binding_id, accepted=reading(amount("5"), 2))
    assert store.record_decision(read_edit, 1, 1, 0, accepted) == 1
    unsaved = store.read_revision(read, 1)
    assert unsaved.fields[addr.binding_id] == baseline
    assert unsaved.decisions[0].decision == accepted
    assert unsaved.decisions[0].actor_id == "preparer-a"
    assert store.read_revision(read, 2).decision_version == 0
    with pytest.raises(TaxAccessDenied):
        store.record_decision(read_edit, 2, 0, 0, MarkReviewed(kind="mark_reviewed"))
    with pytest.raises(TaxConflict, match="decision version"):
        store.record_decision(read_edit, 1, 1, 0, MarkReviewed(kind="mark_reviewed"))
    with pytest.raises(TaxConflict, match="field version"):
        store.record_decision(read_edit, 1, 0, 1, MarkReviewed(kind="mark_reviewed"))
    with pytest.raises(TaxNotFound, match="not linked"):
        store.record_decision(read_edit, 1, 1, 1,
                              ChooseSource(kind="choose_source", binding_id=addr.binding_id, candidate_id="foreign"))
    save = VerifyEdit(kind="verify_edit", binding_id=addr.binding_id, accepted_version=1, saved=reading(amount("5"), 3))
    with pytest.raises(TaxConflict, match="attributable"):
        store.record_decision(grant("review", 1, actor_id="preparer-b"), 1, 1, 1, save)
    with pytest.raises(TaxConflict, match="attributable"):
        store.record_decision(read_edit, 1, 1, 1, save.model_copy(update={"saved": reading(amount("4"), 3)}))
    assert store.record_decision(read_edit, 1, 1, 1, save) == 2
    confirmed = store.read_revision(read, 1)
    assert confirmed.decisions[-1].decision == save
    assert confirmed.fields[addr.binding_id].state.reopened == baseline.state.reopened
    assert confirmed.fields[addr.binding_id].state.last_saved == save.saved
    assert confirmed.changes[-1].previous == baseline.state
    assert confirmed.field_version == 2
    assert store.read_revision(read, 2).fields[addr.binding_id] == baseline
    assert not store.read_revision(read, 2).decisions


def test_rejects_native_rebinding_and_preserves_only_current_assignment(store):
    ingest, read, prepare = grant("ingest"), grant(), grant("prepare", 1)
    store.append_revision(ingest, 0)
    addr = binding("box", "box-1", "row-a")
    row = FieldUpdate(binding=addr, state=FieldState())
    assert store.record_fields(prepare, 1, 0, DESTINATION, (row,)) == 1
    assert store.record_fields(prepare, 1, 1, DESTINATION, (row,)) == 1
    with pytest.raises(TaxConflict, match="field version"):
        store.record_fields(prepare, 1, 0, DESTINATION, (row,))
    with pytest.raises(TaxConflict, match="different verified"):
        store.record_fields(prepare, 1, 1, DESTINATION.model_copy(update={"draft_key": "wrong-draft"}))
    with pytest.raises(TaxConflict, match="identity cannot change"):
        different_row = binding(addr.binding_id, "box-1", "row-b")
        store.record_fields(prepare, 1, 1, DESTINATION, (FieldUpdate(binding=different_row, state=row.state),))
    with pytest.raises(TaxConflict):
        alias = binding("alias", "box-1", "row-a")
        store.record_fields(prepare, 1, 1, DESTINATION, (FieldUpdate(binding=alias, state=row.state),))
    other = grant("ingest", return_id="return-b")
    store.create_return(other, 2025)
    store.append_revision(other, 0)
    with pytest.raises(TaxConflict):
        store.record_fields(grant("prepare", 1, return_id="return-b"), 1, 0, DESTINATION)
    assert store.read_revision(read).fields == {addr.binding_id: row}
    assert store.read_revision(read).field_version == 1


def test_concurrent_successor_writes_cannot_both_use_expected_revision(store):
    ingest = grant("ingest")
    store.append_revision(ingest, 0)
    both_ready = Barrier(2)

    def append(name):
        both_ready.wait(timeout=5)
        try:
            return store.append_revision(ingest, 1, originals=(original(name),))
        except TaxConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(append, ("upload-a", "upload-b"))) == {2, None}
    revision = store.read_revision(grant())
    assert revision.revision == 2 and len(revision.originals) == 1


def test_source_mapping_checks_logical_record_field_native_row_and_both_amounts(store):
    a, b = record(), record("record-b", issuer="employer-b")
    doc_a, doc_b = original(), original("w2-b")
    store.append_revision(grant("ingest"), 0, originals=(doc_a, doc_b), records=(a, b), facts=(
        fact("a-wages", a, doc_a, "box-1", amount("2")),
        fact("a-tax", a, doc_a, "box-2", amount("2")),
        fact("b-wages", b, doc_b, "box-1", amount("2")),
    ))
    addr = binding("wages", "box-1", "row-a", record_id=a.record_id)
    prepare = grant("prepare", 1)
    for fact_id in ("a-tax", "b-wages"):
        with pytest.raises(TaxConflict, match="logical record and field"):
            store.record_fields(prepare, 1, 0, DESTINATION, (update(addr, amount("2"), fact_id),))
    wrong = update(addr, amount("3"), "a-wages")
    with pytest.raises(TaxConflict, match="does not equal"):
        store.record_fields(prepare, 1, 0, DESTINATION, (wrong.model_copy(update={
            "state": wrong.state.model_copy(update={"derivation": "human-readable but unauthorized"}),
        }),))
    correct = update(addr, amount("2.00"), "a-wages")
    with pytest.raises(TaxConflict, match="does not equal"):
        store.record_fields(prepare, 1, 0, DESTINATION, (correct.model_copy(update={
            "state": correct.state.model_copy(update={"entered": reading(amount("12"))}),
        }),))
    assert store.record_fields(prepare, 1, 0, DESTINATION, (correct,)) == 1
    for wrong_row in (
        update(binding("another-field", "box-2", "row-b", record_id=a.record_id), amount("2"), "a-tax"),
        update(binding("other-employer", "box-1", "row-a", record_id=b.record_id), amount("2"), "b-wages"),
    ):
        with pytest.raises(TaxConflict, match="one-to-one"):
            store.record_fields(prepare, 1, 1, DESTINATION, (wrong_row,))


def test_derived_amount_requires_profile_rule_and_exact_inputs(tmp_path):
    ingest, prepare = grant("ingest"), grant("prepare", 1)
    path = tmp_path / "approved-calculation.sqlite3"
    store = TaxStore(path)
    store.ensure_return(ingest, 2025)
    doc, a = original(), record()
    fields = (SourceField(record_id=a.record_id, fact_key="box-1"), SourceField(record_id=a.record_id, fact_key="box-2"))
    store.append_revision(ingest, 0, originals=(doc,), records=(a,), facts=(
        fact("first", a, doc, "box-1", amount("2.10")), fact("second", a, doc, "box-2", amount("3.20")),
    ))
    addr = binding("total", "total").model_copy(update={"source_fields": fields})
    ids = ("first", "second")
    state = FieldState(candidate_ids=ids, used_candidate_ids=ids, proposed=amount("5.30"),
                       derivation="Add the two mapped fields", derivation_rule_id="amount-sum")
    with pytest.raises(TaxConflict, match="approved profile rule"):
        store.record_fields(prepare, 1, 0, DESTINATION, (FieldUpdate(binding=addr, state=state),))
    approved = TaxStore(path, approved_sums={
        (DESTINATION.profile_id, "income", "wages", "total"): ApprovedSum("amount-sum", ("box-1", "box-2")),
    })
    wrong = state.model_copy(update={"proposed": amount("5.31")})
    with pytest.raises(TaxConflict, match="does not match"):
        approved.record_fields(prepare, 1, 0, DESTINATION, (FieldUpdate(binding=addr, state=wrong),))
    assert approved.record_fields(prepare, 1, 0, DESTINATION, (FieldUpdate(binding=addr, state=state),)) == 1
    assert TaxStore(path).read_revision(grant()).fields["total"].state.derivation_rule_id == "amount-sum"


def test_correction_chain_uses_current_source_and_preserves_stale_branch(store):
    ingest, read = grant("ingest"), grant()
    doc, a = original(), record()
    addr = binding("withheld", "box-2", "row-a", record_id=a.record_id)
    key, = addr.source_fields
    store.append_revision(ingest, 0, originals=(doc,), records=(a,), facts=(
        fact("baseline", a, doc, "box-2", amount("30"), raw="Original box 2: 30"),
        fact("unaddressed", a, doc, "box-1", amount("75")),
    ))

    def correct(revision, prefix, before, after, prior):
        pointer = original("w2-a", f"v{revision}")
        identity = a.identity.model_copy(update={"form": "w2c"})
        store.append_revision(ingest, revision - 1, originals=(pointer,), facts=(
            fact(f"{prefix}-old", a, pointer, "box-2", amount(before), column="previously_reported",
                 identity=identity, raw=f"Previously reported box 2: {before}"),
            fact(f"{prefix}-new", a, pointer, "box-2", amount(after), column="correct_information",
                 identity=identity, raw=f"Correct information box 2: {after}", predecessor=prior),
        ))

    correct(2, "first", "30", "0", "baseline")
    with pytest.raises(TaxConflict, match="current eligible"):
        store.record_fields(grant("prepare", 2), 2, 0, DESTINATION, (update(addr, amount("30"), "baseline"),))
    assert store.record_fields(grant("prepare", 2), 2, 0, DESTINATION, (update(addr, amount("0"), "first-new"),)) == 1
    correct(3, "second", "0", "8", "first-new")
    assert store.record_fields(grant("prepare", 3), 3, 0, DESTINATION, (update(addr, amount("8"), "second-new"),)) == 1
    correct(4, "stale", "30", "9", "baseline")
    for candidate, value in (("stale-new", "9"), ("second-new", "8"), ("baseline", "30")):
        with pytest.raises(TaxConflict, match="current eligible"):
            store.record_fields(grant("prepare", 4), 4, 0, DESTINATION, (update(addr, amount(value), candidate),))
    latest = store.read_revision(read)
    assert key not in latest.effective_facts
    assert "stale-new" in latest.candidate_ids_for_binding(addr.binding_id)
    assert latest.fields[addr.binding_id].state.used_candidate_ids == ("second-new",)
    chosen = ChooseSource(kind="choose_source", binding_id=addr.binding_id, candidate_id="stale-new")
    assert store.record_decision(grant("review", 4), 4, 0, 0, chosen) == 1
    assert key not in store.read_revision(read).effective_facts
    assert latest.effective_facts[SourceField(record_id=a.record_id, fact_key="box-1")][0].fact_id == "unaddressed"
    assert latest.facts["stale-new"].raw_value == "Correct information box 2: 9"
    assert latest.fact_revisions["stale-new"] == 4
    assert store.read_revision(read, 1).effective_facts[key][0].fact_id == "baseline"
    assert store.read_revision(read, 3).effective_facts[key][0].fact_id == "second-new"


def test_review_approval_covers_exact_versions_and_preserves_legacy_audit(tmp_path):
    path = tmp_path / "signed.sqlite3"
    store = TaxStore(path)
    ingest, review, prepare, read = grant("ingest"), grant("review", 1), grant("prepare", 1), grant()
    store.ensure_return(ingest, 2025)
    store.append_revision(ingest, 0, originals=(original(),))
    sign = MarkReviewed(kind="mark_reviewed")
    assert store.record_decision(review, 1, 0, 0, sign) == 1
    approved = store.read_revision(read)
    assert (approved.decisions[-1].reviewed_field_version, approved.decisions[-1].reviewed_decision_version) == (0, 0)
    assert not approved.review_required_new_document
    addr = binding("human", "human")
    store.record_fields(prepare, 1, 0, DESTINATION, (FieldUpdate(binding=addr, state=FieldState(reopened=reading(amount("4")))),))
    assert store.read_revision(read).review_required_new_document
    assert store.record_decision(review, 1, 1, 1, sign) == 2
    assert not store.read_revision(read).review_required_new_document
    store.record_decision(review, 1, 1, 2, AcceptEdit(kind="accept_edit", binding_id="human", accepted=reading(amount("5"), 2)))
    assert store.read_revision(read).review_required_new_document
    assert store.record_decision(review, 1, 1, 3, sign) == 4
    assert not store.read_revision(read).review_required_new_document
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE tax_review_decisions DROP COLUMN reviewed_field_version")
        db.execute("ALTER TABLE tax_review_decisions DROP COLUMN reviewed_decision_version")
        db.execute("ALTER TABLE tax_bindings DROP COLUMN source_fields")
    legacy = TaxStore(path).read_revision(read)
    assert legacy.review_required_new_document and legacy.decisions[-1].decision == sign
    assert legacy.decisions[-1].reviewed_field_version is None


def test_postgres_evidence_reuses_outer_transaction_and_serializes_successors(coordinator_db):
    with coordinator_db.transaction() as connection:
        install_tax_schema(connection)
        install_tax_schema(connection)  # Versioned upgrades can safely share the same schema transaction.
    store = PostgresTaxStore(coordinator_db)
    ingest, read, doc, a = grant("ingest"), grant(), original(), record()
    with pytest.raises(RuntimeError, match="abort outer transaction"):
        with coordinator_db.transaction() as connection, store.using_connection(connection) as tx:
            tx.ensure_return(ingest, 2025)
            tx.append_revision(ingest, 0, originals=(doc,), records=(a,), facts=(fact("base", a, doc, "box-1", amount("7")),))
            assert tx.read_revision(read).facts["base"].normalized == amount("7")
            raise RuntimeError("abort outer transaction")
    with pytest.raises(TaxNotFound):
        store.head_revision(ingest)
    with coordinator_db.transaction() as connection, store.using_connection(connection) as tx:
        tx.ensure_return(ingest, 2025)
        tx.ensure_return(ingest, 2025)
        assert tx.head_revision(ingest) == 0
        tx.append_revision(ingest, 0, originals=(doc,), records=(a,), facts=(fact("base", a, doc, "box-1", amount("7")),))
        assert tx.get_original(read, 1, doc.original) == doc
    addr = binding("wages", "box-1", "row-a", record_id=a.record_id)
    assert store.record_fields(grant("prepare", 1), 1, 0, DESTINATION, (update(addr, amount("7"), "base"),)) == 1
    assert store.record_decision(grant("review", 1), 1, 1, 0, MarkReviewed(kind="mark_reviewed")) == 1
    assert not store.read_revision(read, 1).review_required_new_document
    assert store.read_revision(read, 1).fields["wages"].binding == addr
    both_ready = Barrier(2)

    def append(name):
        both_ready.wait(timeout=5)
        try:
            return store.append_revision(ingest, 1, originals=(original(name),))
        except TaxConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(append, ("upload-a", "upload-b"))) == {2, None}
    assert store.read_revision(read).review_required_new_document
