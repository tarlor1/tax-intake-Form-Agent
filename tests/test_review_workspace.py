"""Human decisions, evidence isolation, and controller HTTP boundaries."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from review_fixtures import BUSINESS_ID, INDIVIDUAL_ID, PRIOR_YEAR_ID, populate_store, seeded_store
from src.review_api import create_app
from src.review_store import ReviewError, ReviewStore


def field(detail, identifier):
    return next(item for item in detail["fields"] if item["id"] == identifier)


@pytest.fixture
def store(tmp_path):
    return seeded_store(tmp_path / "workspace")


def test_changing_field_focus_preserves_unresolved_values_and_evidence(store):
    original = store.get_return(INDIVIDUAL_ID)
    selected = store.select(original["id"], original["revision"], "street_address")
    changed = store.select(selected["id"], selected["revision"], "phone")

    assert changed["activeFieldId"] == "phone"
    assert field(changed, "street_address") == field(original, "street_address")
    assert field(changed, "phone") == field(original, "phone")


def test_source_resolution_requires_review_and_preserves_other_facts(store):
    detail = store.get_return(INDIVIDUAL_ID)
    candidate = field(detail, "street_address")["candidates"][-1]
    selected = store.resolve(detail["id"], detail["revision"], "street_address", candidate["id"])
    assert field(selected, "street_address")["status"] == "needs_review"
    assert field(selected, "street_address")["value"] == "28 Sample Avenue"
    assert field(selected, "city") == field(detail, "city")

    focused = store.select(selected["id"], selected["revision"], "email")
    assert field(focused, "street_address") == field(selected, "street_address")


def test_stale_revision_rejects_without_partial_writes(store):
    original = store.get_return(INDIVIDUAL_ID)
    changed = store.select(original["id"], original["revision"], "email")
    with pytest.raises(ReviewError):
        store.correct(original["id"], original["revision"], "phone", "503-555-0101", [])
    assert store.get_return(original["id"]) == changed


def test_candidates_and_citations_belong_to_exact_field_client_and_year(store):
    detail = store.get_return(INDIVIDUAL_ID)
    for other_id, identifier in [
        (PRIOR_YEAR_ID, "email"),
        (BUSINESS_ID, "email"),
        (INDIVIDUAL_ID, "employer"),
    ]:
        other = field(store.get_return(other_id), identifier)
        with pytest.raises(ReviewError):
            store.resolve(detail["id"], detail["revision"], "email", other["candidates"][0]["id"])
        with pytest.raises(ReviewError):
            store.correct(
                detail["id"],
                detail["revision"],
                "email",
                "new@example.test",
                [other["citations"][0]["id"]],
            )
    assert store.get_return(detail["id"]) == detail


def test_restart_retains_review_events_originals_and_legacy_facts(store):
    original = store.get_return(INDIVIDUAL_ID)
    updated = store.correct(
        original["id"],
        original["revision"],
        "number_of_dependents",
        "0",
        [],
        "Verified no dependents",
    )
    updated = store.select(updated["id"], updated["revision"], "phone")
    assert ReviewStore(store.data_dir).get_return(updated["id"]) == updated
    assert updated["legacyFields"] == original["legacyFields"]
    for document in updated["documents"]:
        assert ReviewStore(store.data_dir).get_pdf(updated["id"], document["id"]) == store.get_pdf(
            updated["id"], document["id"]
        )


AUTH = {"X-Review-Token": "local-test-secret"}


@pytest.fixture
def client(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text('<meta name="review-token" content="__REVIEW_TOKEN__">')
    app = create_app(tmp_path / "data", ui, port=8765, token=AUTH["X-Review-Token"])
    populate_store(app.state.store)
    with TestClient(
        app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False
    ) as browser:
        yield browser


def test_api_client_year_edits_are_isolated_and_legacy_facts_are_read_only(client):
    root = f"/api/review/returns/{INDIVIDUAL_ID}"
    current = client.get(root, headers=AUTH).json()
    prior_path = f"/api/review/returns/{PRIOR_YEAR_ID}"
    prior = client.get(prior_path, headers=AUTH).json()
    response = client.post(
        root + "/correct",
        headers=AUTH,
        json={
            "expectedRevision": current["revision"],
            "fieldId": "phone",
            "value": "503-555-0101",
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert field(updated, "phone")["valueOrigin"] == "preparer"
    assert updated["legacyFields"] == current["legacyFields"]
    assert client.get(prior_path, headers=AUTH).json() == prior
    assert (
        client.post(
            root + "/correct",
            headers=AUTH,
            json={
                "expectedRevision": updated["revision"],
                "fieldId": "primary-wages",
                "value": "1",
            },
        ).status_code
        == 404
    )
    assert client.get(root, headers=AUTH).json() == updated


def test_api_authentication_covers_sources_and_injects_only_app_entry(client):
    root = f"/api/review/returns/{INDIVIDUAL_ID}"
    detail = client.get(root, headers=AUTH).json()
    assert client.get("/").status_code == 200
    assert 'content="local-test-secret"' in client.get("/").text
    assert client.get(root).status_code == 401
    document_id = detail["documents"][0]["id"]
    path = root + f"/documents/{document_id}/pdf"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=AUTH)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "headers",
    [{"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"}, {"Origin": "null"}],
)
def test_api_rejects_host_and_origin_even_with_token(client, headers):
    response = client.get("/api/review/returns", headers={**AUTH, **headers})
    assert response.status_code == 403


def test_api_invalid_mutations_remain_atomic(client):
    root = f"/api/review/returns/{INDIVIDUAL_ID}"
    detail = client.get(root, headers=AUTH).json()
    for body in [
        {"fieldId": "urgency", "value": "urgent"},
        {"fieldId": "date_of_birth", "value": "2025-02-30"},
        {"fieldId": "income_sources", "value": '["fake"]'},
        {"fieldId": "phone", "value": "503-555-0101", "citationIds": ["foreign-citation"]},
    ]:
        assert (
            client.post(
                root + "/correct",
                headers=AUTH,
                json={"expectedRevision": detail["revision"], **body},
            ).status_code
            == 422
        )
        assert client.get(root, headers=AUTH).json() == detail
    assert client.get("/%2e%2e/review.sqlite3").status_code == 404


def test_database_failure_is_controlled_and_hides_local_paths(client, monkeypatch):
    def unavailable():
        raise RuntimeError("private filesystem path and credential")

    monkeypatch.setattr(client.app.state.store, "list_returns", unavailable)
    response = client.get("/api/review/returns", headers=AUTH)
    assert response.status_code == 500
    assert "private filesystem" not in response.text


def test_missing_frontend_is_explicit_setup_error(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(tmp_path / "data", tmp_path / "missing-ui")
    assert not (tmp_path / "data").exists()
