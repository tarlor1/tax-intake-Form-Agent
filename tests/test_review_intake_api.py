"""Document-first API boundaries and preparer edits without mandatory explanations."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.review_api import create_app

AUTH = {"X-Review-Token": "document-intake-api-test"}
INTAKE = "/api/review/intake"
RETURNS = "/api/review/returns"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # An empty environment value also prevents a developer's .env from enabling calls.
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text('<meta name="review-token" content="__REVIEW_TOKEN__">')
    app = create_app(tmp_path / "data", ui, port=8765, token=AUTH["X-Review-Token"])
    with TestClient(app, base_url="http://127.0.0.1:8765") as browser:
        yield browser


def upload(client, content, *, name="client.txt", media_type="text/plain"):
    return client.post(
        INTAKE + "/documents",
        content=content,
        headers={**AUTH, "Content-Type": media_type, "X-Document-Name": name},
    )


def current_field(detail, identifier):
    return next(field for field in detail["fields"] if field["id"] == identifier)


def test_upload_does_not_create_clients_or_claim_extraction_without_a_key(client):
    assert client.get(INTAKE).status_code == 401
    response = upload(client, b"Client: Taylor Example\nTax year: 2025\nWages: 1200.00\n")
    assert response.status_code == 201
    document = response.json()
    assert document["status"] == "uploaded"
    assert client.get(RETURNS, headers=AUTH).json() == []
    duplicate = upload(
        client, b"Client: Taylor Example\nTax year: 2025\nWages: 1200.00\n", name="renamed.txt"
    )
    assert duplicate.json()["id"] == document["id"]
    response = client.post(
        INTAKE + "/process",
        headers=AUTH,
        json={
            "documentIds": [document["id"]],
            "approvedForAI": True,
        },
    )
    assert response.status_code == 503
    assert client.get(RETURNS, headers=AUTH).json() == []
    inbox = client.get(INTAKE, headers=AUTH).json()
    assert inbox["configured"] is False
    assert all(group["returnId"] is None for item in inbox["documents"] for group in item["groups"])


def test_external_processing_requires_explicit_boolean_consent(client):
    document = upload(client, b"Client: Jamie Example\nTax year: 2025\n").json()
    response = client.post(
        INTAKE + "/process", headers=AUTH, json={"documentIds": [document["id"]]}
    )
    assert response.status_code == 422
    response = client.post(
        INTAKE + "/process",
        headers=AUTH,
        json={
            "documentIds": [document["id"]],
            "approvedForAI": "true",
        },
    )
    assert response.status_code == 422
    assert client.get(RETURNS, headers=AUTH).json() == []


def test_invalid_or_unsupported_sources_do_not_create_partial_clients(client):
    assert upload(client, b"image", name="photo.jpg", media_type="image/jpeg").status_code == 415
    assert upload(client, b"\xff\xfe\x00invalid", name="broken.txt").status_code == 422
    assert (
        upload(client, b"not a PDF", name="broken.pdf", media_type="application/pdf").status_code
        == 422
    )
    assert client.get(RETURNS, headers=AUTH).json() == []


def test_preparer_can_enter_real_choices_zero_and_clear_without_reason_or_citations(client):
    detail = client.app.state.store.create_return(
        2025, client_name="Manual entry", return_type="individual"
    )
    root = RETURNS + "/" + detail["id"]
    for field_id, value in [
        ("filing_status", "Married filing jointly"),
        ("number_of_dependents", "0"),
        ("income_sources", '["w2_wages","rental"]'),
    ]:
        response = client.post(
            root + "/correct",
            headers=AUTH,
            json={
                "expectedRevision": detail["revision"],
                "fieldId": field_id,
                "value": value,
            },
        )
        assert response.status_code == 200
        detail = response.json()
    assert current_field(detail, "filing_status")["value"] == "married_filing_jointly"
    assert current_field(detail, "number_of_dependents")["value"] == "0"
    assert current_field(detail, "income_sources")["value"] == '["w2_wages","rental"]'
    assert current_field(detail, "income_sources")["valueOrigin"] == "preparer"
    assert current_field(detail, "income_sources")["citations"] == []
    response = client.post(
        root + "/correct",
        headers=AUTH,
        json={
            "expectedRevision": detail["revision"],
            "fieldId": "income_sources",
            "value": "[]",
        },
    )
    assert response.status_code == 200
    cleared = response.json()
    assert current_field(cleared, "income_sources")["value"] == "[]"
    # A stale browser observation must not overwrite the newer edit.
    assert (
        client.post(
            root + "/correct",
            headers=AUTH,
            json={
                "expectedRevision": detail["revision"],
                "fieldId": "income_sources",
                "value": '["rental"]',
            },
        ).status_code
        == 409
    )
    assert client.get(root, headers=AUTH).json() == cleared


def test_text_original_is_scoped_and_upload_alone_cannot_finish_preparation(client):
    store = client.app.state.store
    detail = store.create_return(2025, client_name="Text source owner", return_type="individual")
    other = store.create_return(2025, client_name="Another owner", return_type="individual")
    root = RETURNS + "/" + detail["id"]
    original = "Client: Text source owner\nTax year: 2025\nPhone: 503-555-0101\n"
    response = client.post(
        root + "/documents",
        content=original.encode(),
        headers={
            **AUTH,
            "Content-Type": "text/plain",
            "X-Document-Name": "intake.txt",
            "X-Expected-Revision": str(detail["revision"]),
        },
    )
    assert response.status_code == 200
    detail = response.json()
    document_id = detail["documents"][0]["id"]
    path = "/documents/" + document_id + "/text"
    assert client.get(root + path, headers=AUTH).text == original
    assert client.get(RETURNS + "/" + other["id"] + path, headers=AUTH).status_code == 404
    assert client.get(root + path).status_code == 401
    assert current_field(detail, "phone")["value"] is None
    assert current_field(detail, "urgency")["value"] is None
    assert current_field(detail, "full_name")["valueOrigin"] == "workspace"
