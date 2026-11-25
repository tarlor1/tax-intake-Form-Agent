"""Public API permits scoped evidence and committed ingestion, never native control."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from review_companion_practice import PRIOR, RETURN, REVISION, TOKEN, PracticeCompanion
from review_fixtures import INDIVIDUAL_ID, seeded_store
from src.preparation.review_events import ReviewAccess, ReviewSelectionSession
from src.review_api import _companion_app, create_app

AUTH = {"X-Review-Token": TOKEN}
ROOT = f"/api/review/returns/{RETURN}"
INTAKE = "/api/review/intake"


@pytest.fixture
def ui(tmp_path):
    folder = tmp_path / "ui"
    folder.mkdir()
    (folder / "index.html").write_text('<meta name="review-token" content="__REVIEW_TOKEN__">')
    return folder


@pytest.fixture
def client(ui):
    app = _companion_app(ui, port=8765, token=TOKEN, backend=PracticeCompanion(ui / "archive"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as browser:
        yield browser


def test_deep_link_token_and_protected_revision_and_exact_original(client):
    response = client.get("/clients/client-synthetic/returns/2025")
    assert response.status_code == 200 and f'content="{TOKEN}"' in response.text
    assert "frame-src 'none'" in response.headers["content-security-policy"]
    assert client.get(ROOT + "/evidence").status_code == 401
    detail = client.get(ROOT + "/evidence", headers=AUTH).json()
    assert detail["inputRevision"] == REVISION
    path = ROOT + "/review/originals/doc-b/original-one"
    assert client.get(f"{path}?revision={REVISION}").status_code == 401
    assert client.get(f"{path}?revision={PRIOR}", headers=AUTH).status_code == 404
    assert client.get(f"{path}?revision={REVISION}", headers=AUTH).content.startswith(b"%PDF")
    assert client.get(f"{path.replace(RETURN, 'another-return')}?revision={REVISION}", headers=AUTH).status_code == 404
    assert client.get(f"{path.replace('original-one', 'lost')}?revision={REVISION}", headers=AUTH).status_code == 404


@pytest.mark.parametrize("headers", [
    {"Host": "untrusted.example"}, {"Origin": "https://untrusted.example"}, {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_api_keeps_loopback_host_origin_and_csrf_boundary(client, headers):
    assert client.get("/api/review/returns", headers={**AUTH, **headers}).status_code == 403


def test_old_native_control_and_manual_extraction_routes_are_absent(client):
    for path in ("/api/review/target", ROOT + "/browser", ROOT + "/jobs/job/frame"):
        assert client.get(path, headers=AUTH).status_code == 404
    for path in (
        INTAKE + "/process", ROOT + "/select", ROOT + "/resolve", ROOT + "/correct",
        ROOT + "/proposals", ROOT + "/jobs/job/cancel", ROOT + "/jobs/job/field", ROOT + "/jobs/job/interact",
        ROOT + "/jobs/job/source", ROOT + "/jobs/job/submit",
    ):
        assert client.post(path, headers=AUTH, json={}).status_code in (404, 405)
    assert client.post(ROOT + "/jobs", headers=AUTH, json={}).status_code == 405
    assert client.get(ROOT + "/jobs", headers=AUTH).json()[0]["status"] == "ready_for_review"


def test_batch_approval_is_boolean_and_non_supported_media_is_blocked(client):
    body = {"batchId": "drop", "files": [{"slot": "file", "filename": "source.txt", "mediaType": "text/plain"}]}
    for invalid in (body, {**body, "approvedForAI": "true"}, {**body, "approvedForAI": True, "provider": "unapproved"}):
        assert client.post(INTAKE + "/batches", headers=AUTH, json=invalid).status_code == 422
    assert client.put(INTAKE + "/batches/drop/files/file", content=b"image", headers={
        **AUTH, "Content-Type": "image/jpeg",
    }).status_code == 415


def test_return_upload_scope_rejects_an_unassigned_path_and_mismatched_hint(client):
    body = {
        "batchId": "drop", "approvedForAI": False, "returnHint": "another-return",
        "files": [{"slot": "file", "filename": "source.txt", "mediaType": "text/plain"}],
    }
    path = ROOT + "/intake/batches"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=AUTH).json() == []
    assert client.get(path.replace(RETURN, "another-return"), headers=AUTH).status_code == 404
    assert client.post(path, headers=AUTH, json=body).status_code == 409
    assert client.put(path.replace(RETURN, "another-return") + "/drop/files/file", headers=AUTH).status_code == 404


def test_review_decision_requires_exact_field_version_and_expired_owner_can_recover(client):
    backend = client.app.state.backend
    access = ReviewAccess(backend.scope.firm_id, backend.scope.actor_id, RETURN)
    active = ReviewSelectionSession(
        access, input_revision=REVISION, draft_key="synthetic-draft",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1), bindings={},
    )
    backend.active = active
    body = {
        "kind": "mark_reviewed", "reviewSessionId": active.id, "inputRevision": REVISION,
        "expectedDecisionVersion": 0,
    }
    path = ROOT + "/review"
    assert client.post(path + "/decisions", headers=AUTH, json=body).status_code == 422
    assert client.post(path + "/decisions", headers=AUTH, json={
        **body, "expectedFieldVersion": backend.field_version - 1,
    }).status_code == 409
    assert backend.decisions == 0
    assert client.post(path + "/decisions", headers=AUTH, json={
        **body, "expectedFieldVersion": backend.field_version,
    }).json() == {"decisionVersion": 1}

    active.expires_at = datetime.now(timezone.utc)
    backend.native = SimpleNamespace(is_closed=lambda: False)
    close = {"reviewSessionId": active.id}
    assert client.post(path + "/close", json=close).status_code == 401
    assert client.post(path.replace(RETURN, "another-return") + "/close", headers=AUTH, json=close).status_code == 404
    assert client.post(path + "/close", headers=AUTH, json={"reviewSessionId": "forged"}).status_code == 404
    assert client.post(path + "/close", headers=AUTH, json=close).status_code == 409
    assert client.get(path + "/events", headers={**AUTH, "X-Review-Session": active.id}).status_code == 404
    assert client.post(path + "/open", headers=AUTH, json={"inputRevision": REVISION}).json() == {
        "status": "recovery_required", "reviewSessionId": active.id, "inputRevision": REVISION, "returnId": RETURN,
    }
    backend.native = SimpleNamespace(is_closed=lambda: True)
    assert client.post(path + "/close", headers=AUTH, json=close).status_code == 204
    assert backend.active is None


def test_no_config_only_inspects_existing_workspace_and_does_not_rerun_history(tmp_path, ui):
    data = tmp_path / "legacy"
    store = seeded_store(data)
    before = sha256(store.database_path.read_bytes()).hexdigest()
    app = create_app(data, ui, port=8765, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH) as browser:
        assert not browser.get(INTAKE).json()["enabled"]
        roots = browser.get("/api/review/returns").json()
        assert roots and all(item["destination"] == "Archived intake" for item in roots)
        path = f"/api/review/returns/{INDIVIDUAL_ID}"
        detail = browser.get(path + "/evidence").json()
        assert detail["reviewAvailable"] is False
        assert all(field["status"] != "verified" and field["savedValue"] is None for field in detail["fields"])
        assert browser.get(path + "/jobs").json() == []
        assert browser.post(path + "/review/open", json={"inputRevision": detail["inputRevision"]}).status_code == 409
        assert browser.get(path + "/review/events", headers={**AUTH, "X-Review-Session": "invented"}).status_code == 404
    assert sha256(store.database_path.read_bytes()).hexdigest() == before


def test_missing_ui_does_not_create_workspace(tmp_path):
    with pytest.raises(RuntimeError, match="Review UI is not built"):
        create_app(tmp_path / "data", tmp_path / "missing-ui")
    assert not (tmp_path / "data").exists()


def test_native_factory_requires_enrolled_config(tmp_path, ui):
    with pytest.raises(ValueError, match="enrolled firm configuration"):
        create_app(tmp_path, ui, native_review_factory=lambda *_: pytest.fail("unexpected connector launch"))
