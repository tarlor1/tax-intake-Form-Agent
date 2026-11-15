"""Normalized multi-record draft regressions; never contact a provider."""

from __future__ import annotations

import copy
import json
import threading
import time

import httpx
import pytest
from google import genai

from src.models import IntakeExtraction
from src.review_intake import ReviewIntake, validate_extraction
from src.review_store import ReviewError, ReviewStore


@pytest.fixture
def intake(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key-never-sent")
    monkeypatch.delenv("GEMINI_DEBUG", raising=False)
    store = ReviewStore(tmp_path)
    service = ReviewIntake(store, mutation_lock=threading.RLock(), ensure_idle=lambda _: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Install a synthetic response; real network is forbidden")

    monkeypatch.setattr(service, "_call_gemini", forbidden)
    yield service
    service.close()


def finished(service, document_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        document = next(item for item in service.list_documents() if item["id"] == document_id)
        if document["status"] != "processing":
            return document
        time.sleep(0.01)
    raise AssertionError("Background extraction did not finish")


def record(name="Alex Example", year=2025, **overrides):
    """The original model's required keys, with absent identifiers left absent."""
    payload = {
        "full_name": name,
        "email": None,
        "phone": None,
        "date_of_birth": None,
        "street_address": None,
        "city": None,
        "state": None,
        "zip_code": None,
        "drivers_license_number": None,
        "drivers_license_state": None,
        "drivers_license_expires_on": None,
        "tax_year": year,
        "urgency": {"level": "medium", "reason": "The client requests tax preparation."},
        "missing_fields": [],
        "occupation": "Engineer",
    }
    payload.update(overrides)
    return payload


def group(name="Alex Example", year=2025, *, return_type="individual", **overrides):
    return {
        "clientName": name,
        "returnType": return_type,
        "record": record(name, year, **overrides),
    }


def run_response(service, monkeypatch, groups, *, content=b"Synthetic client correspondence",
                 filename="synthetic.txt"):
    monkeypatch.setattr(service, "_call_gemini",
                        lambda *args, **kwargs: json.dumps({"groups": groups}))
    document = service.upload(filename, content, "text/plain")
    service.process([document["id"]], True)
    return finished(service, document["id"])


def field(detail, name="occupation"):
    return next(item for item in detail["fields"] if item["id"] == name)


def assigned_detail(service, document, index=0):
    return service.store.get_return(document["groups"][index]["returnId"])


def assert_ai_draft(detail, name, value):
    item = field(detail, name)
    assert item["value"] == value
    assert item["valueOrigin"] == "ai"
    assert item["status"] == "needs_review"
    candidate = next(candidate for candidate in item["candidates"] if candidate["value"] == value)
    assert candidate["provenance"] == "gemini"
    assert candidate["documentId"] in {document["id"] for document in detail["documents"]}


def test_no_consent_and_no_key_prevent_all_model_requests(intake, monkeypatch):
    document = intake.upload("synthetic.txt", b"Client correspondence", "text/plain")
    calls = []
    monkeypatch.setattr(intake, "_call_gemini", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ReviewError, match="explicit approval"):
        intake.process([document["id"]], False)
    assert calls == []
    assert intake.list_documents()[0]["status"] == "uploaded"
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(ReviewError, match="GEMINI_API_KEY"):
        intake.process([document["id"]], True)
    assert calls == []
    assert intake.list_documents()[0]["status"] == "failed"
    assert intake.store.list_returns() == []


def test_one_document_contains_multiple_clients_and_multiple_years_without_ssn(intake, monkeypatch):
    content = b"Alex needs this year and last year prepared; Blair is a teacher."
    document = run_response(intake, monkeypatch, [
        group(),
        group("Blair Example", occupation="Teacher"),
        group(year=2024, occupation="Designer"),
    ], content=content)
    assert document["status"] == "completed", document["error"]
    details = [assigned_detail(intake, document, index) for index in range(3)]
    assert len(intake.store.list_returns()) == 3
    assert details[0]["clientId"] == details[2]["clientId"] != details[1]["clientId"]
    assert [detail["taxYear"] for detail in details] == [2025, 2025, 2024]
    for detail, value in zip(details, ["Engineer", "Teacher", "Designer"]):
        assert_ai_draft(detail, "occupation", value)
        assert len(detail["documents"]) == 1
        assert intake.store.get_text(detail["id"], detail["documents"][0]["id"]) == content.decode()
    assert "identityHash" not in json.dumps(document)
    assert intake.upload("renamed.txt", content, "text/plain")["id"] == document["id"]


def test_unique_normalized_name_associates_across_documents_and_years(intake, monkeypatch):
    first = run_response(intake, monkeypatch, [group()], content=b"First correspondence")
    current = assigned_detail(intake, first)
    second = run_response(intake, monkeypatch, [
        group("  ALEX   EXAMPLE  ", occupation="Architect"),
        group("alex example", year=2024),
    ], content=b"Second correspondence")
    assert second["status"] == "completed"
    assert second["groups"][0]["returnId"] == current["id"]
    assert assigned_detail(intake, second, 1)["clientId"] == current["clientId"]
    assert len(intake.store.list_returns()) == 2
    assert len(assigned_detail(intake, second)["documents"]) == 2


@pytest.mark.parametrize("identity_field,first_value,second_value", [
    ("date_of_birth", "1980-04-15", "1981-04-15"),
    ("email", "alex.one@example.com", "alex.two@example.com"),
])
def test_contradictory_details_do_not_merge_same_name_profiles(
    intake, monkeypatch, identity_field, first_value, second_value,
):
    first = run_response(intake, monkeypatch, [group(**{identity_field: first_value})],
                         content=b"First profile")
    original = assigned_detail(intake, first)
    second = run_response(intake, monkeypatch, [group(**{identity_field: second_value})],
                          content=b"Distinct profile with the same name")
    assert second["status"] == "needs_assignment"
    assert second["groups"][0]["returnId"] is None
    assert second["groups"][0]["issues"]
    assert len(intake.store.list_returns()) == 1
    assert field(intake.store.get_return(original["id"]), identity_field)["value"] == first_value


def test_exact_identity_matches_known_client_even_when_name_is_ambiguous(intake, monkeypatch):
    document = run_response(intake, monkeypatch, [
        group(email="alex.one@example.com"),
    ], content=b"First known profile")
    original = assigned_detail(intake, document)
    other = intake.store.create_return(2025, client_name="Alex Example", return_type="individual")
    intake.store.correct(other["id"], other["revision"], "email", "alex.two@example.com", [], "")
    followup = run_response(intake, monkeypatch, [
        group(year=2024, email="alex.one@example.com"),
    ], content=b"Prior year for the first profile")
    assert followup["status"] == "completed"
    assert assigned_detail(intake, followup)["clientId"] == original["clientId"]


def test_ambiguous_same_name_requires_explicit_assignment(intake, monkeypatch):
    chosen = intake.store.create_return(2025, client_name="Alex Example", return_type="individual")
    intake.store.create_return(2025, client_name="Alex Example", return_type="individual")
    document = run_response(intake, monkeypatch, [group()], content=b"No distinguishing details")
    assert document["status"] == "needs_assignment"
    assert document["groups"][0]["returnId"] is None
    assert document["groups"][0]["issues"]
    assert len(intake.store.list_returns()) == 2
    assigned = intake.assign(document["id"], document["groups"][0]["id"], return_id=chosen["id"])
    assert assigned["status"] == "completed"
    assert assigned["groups"][0]["returnId"] == chosen["id"]


def test_same_name_different_return_type_does_not_merge_clients(intake, monkeypatch):
    document = run_response(intake, monkeypatch, [group(), group(return_type="business")])
    assert document["status"] == "completed"
    assert assigned_detail(intake, document)["clientId"] != assigned_detail(intake, document, 1)["clientId"]


@pytest.mark.parametrize("missing", ["name", "year", "type"])
def test_missing_assignment_metadata_is_durable_and_manually_assignable(intake, monkeypatch, missing):
    extracted = group()
    if missing == "name":
        extracted["clientName"] = None
        extracted["record"]["full_name"] = None
    elif missing == "year":
        extracted["record"]["tax_year"] = None
    else:
        extracted["returnType"] = None
    document = run_response(intake, monkeypatch, [extracted])
    assert document["status"] == "needs_assignment"
    assert intake.store.list_returns() == []
    intake.close()
    reopened = ReviewIntake(intake.store, mutation_lock=threading.RLock(), ensure_idle=lambda _: None)
    try:
        recovered = reopened.list_documents()[0]
        assigned = reopened.assign(
            recovered["id"], recovered["groups"][0]["id"],
            client_name="Alex Example", tax_year=2025, return_type="individual",
        )
        assert assigned["status"] == "completed"
        assert_ai_draft(assigned_detail(reopened, assigned), "occupation", "Engineer")
    finally:
        reopened.close()


def test_original_normalization_and_semantic_summaries_import_without_literal_gates(intake, monkeypatch):
    extracted = group(
        date_of_birth="1994-03-14", state="Oregon", drivers_license_state="Texas",
        filing_status="MFJ", marital_status="MARRIED", preferred_contact_method="EMAIL",
        income_sources=["rental", "w2_wages", "rental"],
        deductions=["charitable", "mortgage_interest"],
        number_of_dependents=0, consultation_requested=True, irs_notice_received=False,
        deadline_date="2026-04-15",
        urgency={"level": "high", "reason": "The client faces a near-term filing deadline."},
        request_summary="Prepare the joint return and discuss rental income.",
    )
    original = IntakeExtraction.model_validate(extracted["record"])
    assert original.state == "OR"
    normalized = validate_extraction({"groups": [extracted]})
    assert normalized[0]["issues"] == []
    assert all(set(value) == {"fieldId", "value"} for value in normalized[0]["values"])
    document = run_response(
        intake, monkeypatch, [extracted],
        content=b"Born March 14, 1994 in Oregon. Married; please email us about filing together. "
                b"We have a W-2 and rent out a house; can we talk before the April deadline?",
    )
    assert document["status"] == "completed"
    detail = assigned_detail(intake, document)
    for name, expected in {
        "date_of_birth": "1994-03-14", "state": "OR", "drivers_license_state": "TX",
        "filing_status": "married_filing_jointly", "marital_status": "married",
        "preferred_contact_method": "email", "income_sources": '["w2_wages","rental"]',
        "deductions": '["mortgage_interest","charitable"]', "number_of_dependents": "0",
        "consultation_requested": "yes", "irs_notice_received": "no",
        "deadline_date": "2026-04-15", "urgency": "high",
        "request_summary": "Prepare the joint return and discuss rental income.",
    }.items():
        assert_ai_draft(detail, name, expected)
    assert field(detail, "drivers_license_number")["value"] is None


def test_corrected_source_preserves_preparer_decision_and_conflicting_ai_drafts(intake, monkeypatch):
    document = run_response(intake, monkeypatch, [group()], content=b"Original correspondence")
    detail = assigned_detail(intake, document)
    original_candidate = copy.deepcopy(field(detail)["candidates"][0])
    detail = intake.store.correct(detail["id"], detail["revision"], "occupation", "Consultant", [], "")
    corrected = run_response(intake, monkeypatch, [group(occupation="Architect")],
                             content=b"Corrected correspondence", filename="corrected.txt")
    assert corrected["groups"][0]["returnId"] == detail["id"]
    updated = assigned_detail(intake, corrected)
    primary = field(updated)
    assert primary["value"] == "Consultant"
    assert primary["valueOrigin"] == "preparer"
    assert primary["citations"] == []
    assert primary["conflict"]
    assert primary["candidates"][0] == original_candidate
    assert {candidate["value"] for candidate in primary["candidates"]} == {"Engineer", "Architect"}
    assert len({candidate["documentId"] for candidate in primary["candidates"]}) == 2
    selected = intake.store.resolve(updated["id"], updated["revision"], "occupation", original_candidate["id"])
    assert_ai_draft(selected, "occupation", "Engineer")


def test_invalid_original_record_retries_once_with_validation_feedback(intake, monkeypatch):
    calls = []

    def response(pages, *args, **kwargs):
        calls.append((pages, args, kwargs))
        return json.dumps({"groups": [group(state="Ontario") if len(calls) == 1 else group(state="Oregon")]})

    monkeypatch.setattr(intake, "_call_gemini", response)
    uploaded = intake.upload("synthetic.txt", b"Client correspondence", "text/plain")
    intake.process([uploaded["id"]], True)
    document = finished(intake, uploaded["id"])
    assert document["status"] == "completed", document["error"]
    assert len(calls) == 2
    assert "state" in json.dumps(calls[1][1:])
    assert_ai_draft(assigned_detail(intake, document), "state", "OR")


def test_two_invalid_original_records_fail_visibly_without_partial_import(intake, monkeypatch):
    calls = []

    def response(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({"groups": [group("Valid Client"), group(state="Ontario")]})

    monkeypatch.setattr(intake, "_call_gemini", response)
    uploaded = intake.upload("invalid.txt", b"Two clients", "text/plain")
    intake.process([uploaded["id"]], True)
    document = finished(intake, uploaded["id"])
    assert len(calls) == 2
    assert document["status"] == "failed"
    assert document["error"]
    assert "review" in document["error"].lower()
    assert intake.store.list_returns() == []


def test_provider_failure_is_safe_and_does_not_stop_another_file(intake, monkeypatch):
    calls = []

    def response(pages, *args, **kwargs):
        text = pages[0]["lines"][0]["text"]
        calls.append(text)
        if text == "Failure Example":
            raise RuntimeError("provider echoed secret-key and source-data")
        return json.dumps({"groups": [group()]})

    monkeypatch.setattr(intake, "_call_gemini", response)
    bad = intake.upload("bad.txt", b"Failure Example", "text/plain")
    good = intake.upload("good.txt", b"Good Example", "text/plain")
    intake.process([bad["id"], good["id"]], True)
    failed = finished(intake, bad["id"])
    assert failed["status"] == "failed"
    assert "secret-key" not in failed["error"]
    assert "source-data" not in failed["error"]
    assert finished(intake, good["id"])["status"] == "completed"
    assert sorted(calls) == ["Failure Example", "Good Example"]
    assert len(intake.store.list_returns()) == 1


def test_active_processing_is_idempotent_and_queue_capacity_is_bounded(intake, monkeypatch):
    monkeypatch.setattr("src.review_intake.MAX_ACTIVE_DOCUMENTS", 3)
    release, entered = threading.Event(), threading.Event()
    calls = []

    def response(pages, *args, **kwargs):
        calls.append(pages[0]["lines"][0]["text"])
        entered.set()
        assert release.wait(5)
        raise ReviewError(502, "Synthetic provider failure")

    monkeypatch.setattr(intake, "_call_gemini", response)
    documents = [
        intake.upload(f"source-{index}.txt", f"synthetic document {index}".encode(), "text/plain")
        for index in range(4)
    ]
    try:
        intake.process([item["id"] for item in documents[:2]], True)
        assert entered.wait(2)
        assert intake.process([documents[0]["id"]], True)[0]["status"] == "processing"
        intake.process([documents[2]["id"]], True)
        with pytest.raises(ReviewError) as error:
            intake.process([documents[3]["id"]], True)
        assert error.value.status_code == 429
    finally:
        release.set()
    for document in documents[:3]:
        finished(intake, document["id"])
    assert sorted(calls) == [f"synthetic document {index}" for index in range(3)]


def test_interrupted_processing_requires_renewed_consent_without_network(intake):
    document = intake.upload("interrupted.txt", b"Synthetic source", "text/plain")
    with intake.store._connection() as connection:
        document["status"] = "processing"
        connection.execute("UPDATE review_intake_documents SET payload = ? WHERE id = ?",
                           (json.dumps(document), document["id"]))
        connection.commit()
    intake.close()
    reopened = ReviewIntake(intake.store, mutation_lock=threading.RLock(), ensure_idle=lambda _: None)
    try:
        recovered = reopened.list_documents()[0]
        assert recovered["status"] == "failed"
        assert "Approve" in recovered["error"]
        assert reopened.store.list_returns() == []
    finally:
        reopened.close()


@pytest.mark.parametrize("empty_groups", [False, True], ids=["assigned", "empty"])
def test_legacy_extraction_requires_new_consent_and_preserves_previous_results(
    intake, monkeypatch, empty_groups,
):
    content = b"Original client correspondence"
    document = run_response(intake, monkeypatch, [group()], content=content)
    detail = assigned_detail(intake, document)
    return_id = detail["id"]
    source_id = detail["documents"][0]["id"]
    with intake.store._connection() as connection:
        _, historical = intake._get(connection, document["id"])
        historical.pop("_intakeContractVersion")
        if empty_groups:
            historical["groups"] = []
            historical["_extractedGroups"] = []
        previous_groups = copy.deepcopy(historical["groups"])
        previous_extraction = copy.deepcopy(historical["_extractedGroups"])
        intake._save(connection, historical)
        detail.pop("intakeContractVersion")
        connection.execute("UPDATE review_returns SET payload = ? WHERE id = ?",
                           (json.dumps(detail), return_id))
        connection.commit()
    intake.close()
    reopened = ReviewIntake(intake.store, mutation_lock=threading.RLock(), ensure_idle=lambda _: None)
    calls = []

    def extract_again(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({"groups": [group()]})

    monkeypatch.setattr(reopened, "_call_gemini", extract_again)
    try:
        recovered = reopened.list_documents()[0]
        assert recovered["status"] == "uploaded"
        assert recovered["groups"] == []
        assert calls == []
        assert reopened.store.get_text(return_id, source_id) == content.decode()
        with reopened.store._connection() as connection:
            _, preserved = reopened._get(connection, document["id"])
        assert preserved["_legacyExtractions"] == [{
            "groups": previous_groups, "extractedGroups": previous_extraction,
        }]
        current = reopened.store.get_return(return_id)
        assert current["documents"][0]["extractionStatus"] == "pending"
        with pytest.raises(ReviewError):
            reopened.process_return(return_id, current["revision"], False)
        assert calls == []
        reopened.process_return(return_id, current["revision"], True)
        refreshed = finished(reopened, document["id"])
        assert refreshed["status"] == "completed"
        assert refreshed["groups"][0]["returnId"] == return_id
        assert len(calls) == 1
        assert_ai_draft(reopened.store.get_return(return_id), "occupation", "Engineer")
    finally:
        reopened.close()


def use_real_gemini_transport(intake, monkeypatch, handler):
    real_client = genai.Client

    def client_with_transport(**kwargs):
        kwargs["http_options"].client_args = {"transport": httpx.MockTransport(handler)}
        return real_client(**kwargs)

    monkeypatch.setattr(genai, "Client", client_with_transport)
    monkeypatch.setattr(intake, "_call_gemini", ReviewIntake._call_gemini.__get__(intake, ReviewIntake))


@pytest.mark.parametrize("debug_setting", [None, "0", "1"])
def test_real_sdk_nested_schema_wire_and_opt_in_raw_response_logging(
    intake, monkeypatch, caplog, debug_setting,
):
    if debug_setting is not None:
        monkeypatch.setenv("GEMINI_DEBUG", debug_setting)
    private_summary = "Private response marker: prepare the joint return."
    result = {"groups": [group(request_summary=private_summary)]}
    requests = []

    def provider(request):
        requests.append(request)
        body = json.loads(request.content)
        config = body["generationConfig"]
        schema = config["responseJsonSchema"]
        assert schema["additionalProperties"] is False
        assert "responseSchema" not in config
        encoded_schema = json.dumps(schema)
        for bound in ("minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"):
            assert f'"{bound}":' not in encoded_schema
        assert '"record"' in encoded_schema
        assert '"missing_fields"' in encoded_schema
        assert '"urgency"' in encoded_schema
        assert '"scopes"' not in encoded_schema
        assert '"nameEvidence"' not in encoded_schema
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": json.dumps(result)}]},
                "finishReason": "STOP",
            }],
        })

    use_real_gemini_transport(intake, monkeypatch, provider)
    uploaded = intake.upload("synthetic.txt", b"Private input marker: client correspondence", "text/plain")
    intake.process([uploaded["id"]], True)
    document = finished(intake, uploaded["id"])
    assert document["status"] == "completed", document["error"]
    assert len(requests) == 1
    assert_ai_draft(assigned_detail(intake, document), "request_summary", private_summary)
    assert (private_summary in caplog.text) is (debug_setting == "1")
    assert "Private input marker" not in caplog.text
    assert "synthetic-test-key-never-sent" not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 503])
def test_gemini_http_failures_report_status_without_leaking_provider_payload(
    intake, monkeypatch, caplog, status,
):
    secret = "synthetic-test-key-never-sent"
    private_text = "private source content must not appear in errors"
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {
            "code": status, "status": "SYNTHETIC_PROVIDER_ERROR",
            "message": f"{secret}: {private_text}",
        }})

    use_real_gemini_transport(intake, monkeypatch, provider)
    uploaded = intake.upload("synthetic.txt", b"Client correspondence", "text/plain")
    intake.process([uploaded["id"]], True)
    failed = finished(intake, uploaded["id"])
    assert failed["status"] == "failed"
    assert f"HTTP {status}" in failed["error"]
    assert len(requests) == 1
    assert secret not in failed["error"] + caplog.text
    assert private_text not in failed["error"] + caplog.text
    assert intake.store.list_returns() == []


def test_prepare_return_waits_for_complete_batch_and_deduplicates_latest_revision(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key-never-sent")
    store = ReviewStore(tmp_path)
    release, entered, prepared = threading.Event(), threading.Event(), threading.Event()
    calls = []
    service = None

    def prepare(return_id):
        detail = store.get_return(return_id)
        calls.append((return_id, detail["revision"], len(detail["documents"]),
                      [document["status"] for document in service.list_documents()]))
        prepared.set()

    service = ReviewIntake(store, mutation_lock=threading.RLock(), ensure_idle=lambda _: None,
                           prepare_return=prepare)

    def response(pages, *args, **kwargs):
        if pages[0]["lines"][0]["text"] == "Second document":
            entered.set()
            assert release.wait(5)
        return json.dumps({"groups": [group()]})

    monkeypatch.setattr(service, "_call_gemini", response)
    first = service.upload("first.txt", b"First document", "text/plain")
    second = service.upload("second.txt", b"Second document", "text/plain")
    try:
        service.process([first["id"], second["id"]], True)
        assert entered.wait(2)
        finished(service, first["id"])
        assert calls == []
        release.set()
        finished(service, second["id"])
        assert prepared.wait(2)
        assert len(calls) == 1
        return_id, revision, document_count, statuses = calls[0]
        assert document_count == 2
        assert statuses == ["completed", "completed"]
        assert revision == store.get_return(return_id)["revision"]
    finally:
        release.set()
        service.close()


def test_manual_assignment_prepares_return_and_callback_failures_are_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key-never-sent")
    calls = []
    service = None

    def prepare(return_id):
        calls.append(service.store.get_return(return_id))
        raise ReviewError(503, "Synthetic browser preparation unavailable")

    service = ReviewIntake(ReviewStore(tmp_path), mutation_lock=threading.RLock(),
                           ensure_idle=lambda _: None, prepare_return=prepare)
    try:
        document = run_response(service, monkeypatch, [group(return_type=None)])
        assert document["status"] == "needs_assignment"
        assert calls == []
        document = service.assign(document["id"], document["groups"][0]["id"],
                                  return_type="individual")
        assert len(calls) == 1
        assert calls[0]["id"] == document["groups"][0]["returnId"]
        assert calls[0]["revision"] == service.store.get_return(calls[0]["id"])["revision"]
        assert any("prepar" in issue.lower() or "browser" in issue.lower()
                   for issue in document["groups"][0]["issues"])
    finally:
        service.close()
