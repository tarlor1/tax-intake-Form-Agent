"""User-visible routing, upload, and queue-ownership boundaries.

These API checks deliberately do not enter the application lifespan. Real
Playwright execution is exercised against the running service in UI verification;
queued jobs here stay queued so ownership checks are deterministic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.review_api import create_app
from review_fixtures import INDIVIDUAL_ID, PRIOR_YEAR_ID, populate_store, source

AUTH = {"X-Review-Token": "prototype-test-token"}
ROOT = f"/api/review/returns/{INDIVIDUAL_ID}"


@pytest.fixture
def client(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text(
        '<html><head><meta name="review-token" content="__REVIEW_TOKEN__"></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    app = create_app(tmp_path / "data", ui, port=8765, token="prototype-test-token")
    populate_store(app.state.store)
    browser = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    try:
        yield browser
    finally:
        browser.close()
        app.state.jobs.close()


def get_detail(client):
    response = client.get(ROOT, headers=AUTH)
    assert response.status_code == 200
    return response.json()


def upload(client, detail, pdf):
    return client.post(
        ROOT + "/documents",
        content=pdf,
        headers={
            **AUTH,
            "Content-Type": "application/pdf",
            "X-Document-Name": "Additional%20source.pdf",
            "X-Expected-Revision": str(detail["revision"]),
        },
    )


def test_client_year_deep_link_serves_app_without_swallowing_api_errors(client):
    before = get_detail(client)
    response = client.get("/clients/avery-morgan/returns/2025?field=email")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'content="prototype-test-token"' in response.text
    assert get_detail(client) == before
    for path in ("/api/review/unknown", "/assets/missing.js", "/missing-page"):
        assert client.get(path, headers=AUTH).status_code == 404


def test_passive_field_selection_never_accepts_previous_field(client):
    before = get_detail(client)
    response = client.post(
        ROOT + "/select",
        headers=AUTH,
        json={
            "expectedRevision": before["revision"],
            "fieldId": "email",
        },
    )
    assert response.status_code == 200
    after = response.json()
    assert after["activeFieldId"] == "email"
    assert after["fields"] == before["fields"]
    assert not any(event["kind"] == "accepted" for event in after["events"])
    stale = client.post(
        ROOT + "/select",
        headers=AUTH,
        json={
            "expectedRevision": before["revision"],
            "fieldId": "phone",
        },
    )
    assert stale.status_code == 409
    assert get_detail(client) == after


def test_pdf_upload_and_manual_proposal_use_real_scoped_source_text(client):
    before = get_detail(client)
    _, pdf, _ = source(
        "uploaded-test",
        "Additional client source",
        [
            "Client: Avery Morgan | Tax year: 2025",
            "Phone: 503-555-0111",
        ],
    )
    response = upload(client, before, pdf)
    assert response.status_code == 200
    uploaded = response.json()
    document = next(
        item
        for item in uploaded["documents"]
        if item["id"] not in {old["id"] for old in before["documents"]}
    )
    assert uploaded["fields"] == before["fields"]
    duplicate = upload(client, uploaded, pdf)
    assert duplicate.status_code == 200
    assert duplicate.json()["documents"] == uploaded["documents"]
    assert duplicate.json()["fields"] == uploaded["fields"]
    uploaded = duplicate.json()

    response = client.post(
        ROOT + "/proposals",
        headers=AUTH,
        json={
            "expectedRevision": uploaded["revision"],
            "fieldId": "phone",
            "documentId": document["id"],
            "page": 1,
            "rect": [0.0, 0.0, 1.0, 1.0],
            "value": "503-555-0111",
            "reason": "Compared the selected source with the phone field.",
        },
    )
    assert response.status_code == 200
    updated = response.json()
    phone = next(field for field in updated["fields"] if field["id"] == "phone")
    assert phone["value"] == "503-555-0111"
    assert phone["status"] == "needs_review"
    assert phone["citations"][0]["documentId"] == document["id"]
    assert "503-555-0111" in phone["citations"][0]["quote"]
    assert (
        client.get(
            f"/api/review/returns/{PRIOR_YEAR_ID}/documents/{document['id']}/pdf",
            headers=AUTH,
        ).status_code
        == 404
    )

    stale_upload = upload(client, before, pdf)
    assert stale_upload.status_code == 409
    assert get_detail(client) == updated


def test_queued_job_blocks_review_changes_until_cancelled(client):
    before = get_detail(client)
    response = client.post(
        ROOT + "/jobs",
        headers=AUTH,
        json={
            "expectedRevision": before["revision"],
        },
    )
    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "queued"
    duplicate = client.post(
        ROOT + "/jobs",
        headers=AUTH,
        json={
            "expectedRevision": before["revision"],
        },
    )
    assert duplicate.status_code == 409
    select = {"expectedRevision": before["revision"], "fieldId": "email"}
    assert client.post(ROOT + "/select", headers=AUTH, json=select).status_code == 409
    assert get_detail(client) == before
    cancelled = client.post(ROOT + f"/jobs/{job['id']}/cancel", headers=AUTH, json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post(ROOT + "/select", headers=AUTH, json=select).status_code == 200
