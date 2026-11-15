"""Synthetic model responses, real API/queue and unchanged Chromium intake forms."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
import uvicorn

from src.form_server import IntakeServer, _IntakeHandler
from src.models import IntakeExtraction
from src.review_api import create_app
from src.review_target import form_payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH = {"X-Review-Token": "automatic-session-test"}
INTAKE = "/api/review/intake"
RETURNS = "/api/review/returns"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Install Chromium with python -m playwright install chromium")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key-never-sent")
    requests = []
    original_post = _IntakeHandler.do_POST

    def record_post(handler):
        requests.append(("POST", handler.path))
        return original_post(handler)

    monkeypatch.setattr(_IntakeHandler, "do_POST", record_post)
    ui = PROJECT_ROOT / "review_ui" / "dist"
    if not (ui / "index.html").is_file():
        pytest.skip("Build review_ui before running automatic-session UI tests")
    with IntakeServer(PROJECT_ROOT / "intake_form") as target, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(
            tmp_path / "workspace",
            ui,
            port=port,
            token=AUTH["X-Review-Token"],
            target_url=target.url + "index.html",
            max_workers=1,
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("Install a synthetic response; external model calls are forbidden")

        monkeypatch.setattr(app.state.intake, "_call_gemini", forbidden)
        with TestClient(app, base_url=f"http://127.0.0.1:{port}", headers=AUTH) as client:
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            try:
                wait_until(lambda: server.started)
                yield client, requests
            finally:
                server.should_exit = True
                thread.join(timeout=15)
                assert not thread.is_alive()


def group(name, year, occupation, *, email="synthetic@example.test"):
    record = {
        key: None for key, field in IntakeExtraction.model_fields.items() if field.is_required()
    }
    record.update(
        full_name=name,
        tax_year=year,
        email=email,
        date_of_birth="1985-02-12",
        state="Oregon",
        occupation=occupation,
        filing_status="Single",
        income_sources=["w2_wages", "rental"],
        consultation_requested=True,
        urgency={"level": "low", "reason": "Routine synthetic preparation request."},
        missing_fields=[],
    )
    return {"clientName": name, "returnType": "individual", "record": record}


def wait_until(check, timeout=60):
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        pause.wait(0.04)
    pytest.fail("Automatic original-form browser session did not reach its observable state")


def extract(
    client,
    monkeypatch,
    groups,
    *,
    content=b"Synthetic consolidated tax preparation correspondence for multiple clients.\n",
):
    calls = []

    def response(*args, **kwargs):
        calls.append(True)
        return json.dumps({"groups": groups})

    monkeypatch.setattr(client.app.state.intake, "_call_gemini", response)
    uploaded = client.post(
        INTAKE + "/documents",
        headers={"Content-Type": "text/plain", "X-Document-Name": "synthetic-clients.txt"},
        content=content,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    processed = client.post(
        INTAKE + "/process",
        json={"documentIds": [document_id], "approvedForAI": True},
    )
    assert processed.status_code == 200, processed.text

    def finished():
        inbox = client.get(INTAKE).json()
        document = next(item for item in inbox["documents"] if item["id"] == document_id)
        assert document["status"] != "failed", document
        return document if document["status"] == "completed" else None

    document = wait_until(finished)
    assert len(calls) == 1
    return document


def ready(client, return_id):
    def check():
        response = client.get(f"{RETURNS}/{return_id}/jobs")
        assert response.status_code == 200, response.text
        jobs = response.json()
        if not jobs:
            return None
        assert len(jobs) == 1, jobs
        job = jobs[0]
        assert job["status"] not in {"failed", "interrupted", "cancelled"}, job
        return job if job["status"] == "ready_for_review" and job["browserAvailable"] else None

    return wait_until(check)


def test_one_document_automatically_fills_two_separate_client_year_browsers(workspace, monkeypatch):
    client, requests = workspace
    document = extract(
        client,
        monkeypatch,
        [
            group("Avery Morgan", 2024, "Engineer", email="avery@example.test"),
            group("Blair Chen", 2025, "Designer", email="blair@example.test"),
        ],
    )
    assert len(document["groups"]) == 2
    returns = client.get(RETURNS).json()
    assert {(item["clientName"], item["taxYear"]) for item in returns} == {
        ("Avery Morgan", 2024), ("Blair Chen", 2025)
    }
    assert len({item["clientId"] for item in returns}) == 2
    jobs = []
    for summary in returns:
        return_id = summary["id"]
        job = ready(client, return_id)
        jobs.append(job)
        detail = client.get(f"{RETURNS}/{return_id}").json()
        state = client.get(f"{RETURNS}/{return_id}/browser").json()
        assert state["values"] == form_payload(detail)
        assert state["values"]["full_name"] == summary["clientName"]
        assert state["values"]["tax_year"] == str(summary["taxYear"])
        assert state["values"]["state"] == "OR"
        assert state["values"]["consultation_requested"] == "yes"
        assert state["values"]["income_sources"] == ["w2_wages", "rental"]
        assert not state["submitted"] and state["intakeId"] is None
        assert job["sourceRevision"] == detail["revision"]
        assert job["completedFields"] == job["totalFields"]
        assert job["target"] == "intake-form"
        occupation = next(item for item in detail["fields"] if item["id"] == "occupation")
        assert occupation["valueOrigin"] == "ai" and occupation["status"] == "needs_review"
        assert occupation["citations"] == []
    assert len({job["id"] for job in jobs}) == 2
    # One automation slot still prepares both while retaining separate review browsers.
    assert all(client.get(f"{RETURNS}/{item['id']}/browser").json()["browserAvailable"] for item in returns)
    assert requests == []


def test_queued_ai_choice_commits_only_after_actual_browser_readback(workspace, monkeypatch):
    client, requests = workspace
    document = extract(
        client,
        monkeypatch,
        [group("Avery Morgan", 2025, "Engineer"), group("Avery Morgan", 2025, "Architect")],
    )
    return_ids = {item["returnId"] for item in document["groups"]}
    assert len(return_ids) == 1 and None not in return_ids
    return_id = return_ids.pop()
    job = ready(client, return_id)
    root = f"{RETURNS}/{return_id}"
    detail = client.get(root).json()
    occupation = next(item for item in detail["fields"] if item["id"] == "occupation")
    assert occupation["conflict"]
    assert client.get(root + "/browser").json()["values"]["occupation"] == ""
    chosen = next(item for item in occupation["candidates"] if item["value"] == "Architect")
    assert chosen["citations"] == [] and chosen["provenance"] == "gemini"
    assert chosen["documentId"] in {item["id"] for item in detail["documents"]}
    before_events = len(detail["events"])
    # Holding the queue lock makes the pre-readback state deterministic without a fake worker.
    with client.app.state.jobs.lock:
        pending = client.app.state.jobs.choose_source(
            return_id, job["id"], detail["revision"], "occupation", chosen["id"],
            reason="Reviewed normalized AI draft against original document",
        )
        assert pending["actionPending"]
        assert client.app.state.store.get_return(return_id) == detail

    def acknowledged():
        current = client.get(root + "/jobs").json()[0]
        assert current["browserAvailable"], current
        if current["actionPending"]:
            return None
        assert current["actionError"] is None, current
        return current

    wait_until(acknowledged)
    changed = client.get(root).json()
    occupation = next(item for item in changed["fields"] if item["id"] == "occupation")
    state = client.get(root + "/browser").json()
    assert state["values"]["occupation"] == occupation["value"] == "Architect"
    assert occupation["selectedCandidateId"] == chosen["id"]
    assert occupation["valueOrigin"] == "ai" and occupation["status"] == "needs_review"
    assert occupation["citations"] == [] and not occupation["conflict"]
    assert changed["revision"] == detail["revision"] + 1
    events = changed["events"][before_events:]
    assert any(event["kind"] == "source_resolved" and "AI draft" in event["message"] for event in events)
    assert not any(event["kind"] == "manual_correction" for event in events)
    assert not state["submitted"] and state["intakeId"] is None
    assert requests == []


def test_ai_draft_review_opens_original_without_fabricated_quote(workspace, monkeypatch, tmp_path):
    client, requests = workspace
    document = extract(client, monkeypatch, [group("Avery Morgan", 2025, "Engineer")])
    return_id = document["groups"][0]["returnId"]
    ready(client, return_id)
    detail = client.get(f"{RETURNS}/{return_id}").json()
    origin = str(client.base_url).rstrip("/")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.goto(f"{origin}/clients/{detail['clientId']}/returns/2025")
        page.locator("#field-navigation").select_option("occupation")
        expect(page.get_by_text("AI-extracted draft · pending review", exact=True)).to_be_visible()
        page.get_by_role("button", name=re.compile("Open original document")).click()
        expect(page.locator(".text-evidence-viewport")).to_contain_text(
            "Synthetic consolidated tax preparation correspondence"
        )
        expect(page.locator("#actual-browser-viewport img")).to_be_visible()
        assert page.locator(".evidence-highlight").count() == 0
        image = tmp_path / "ai-draft-review.png"
        page.screenshot(path=str(image), full_page=True)
        print(f"AI draft review screenshot: {image}")
        browser.close()
    assert requests == []


def test_whole_field_api_reads_back_controls_before_recording_preparer_values(workspace, monkeypatch):
    client, requests = workspace
    document = extract(client, monkeypatch, [group("Avery Morgan", 2025, "Engineer")])
    return_id = document["groups"][0]["returnId"]
    job = ready(client, return_id)
    root = f"{RETURNS}/{return_id}"

    edits = [
        ("occupation", "Engineer"),
        ("email", "reviewed@example.test"),
        ("date_of_birth", "1987-03-14"),
        ("state", "CA"),
        ("number_of_dependents", "0"),
        ("income_sources", '["rental"]'),
        ("income_sources", "[]"),
        ("notes", "Reviewed original.\nClient confirmed correction."),
        ("notes", ""),
    ]
    for field_id, value in edits:
        before = client.app.state.store.get_return(return_id)
        with client.app.state.jobs.lock:
            queued = client.app.state.jobs.apply_field(
                return_id, job["id"], before["revision"], field_id, value
            )
            assert queued["actionPending"]
            assert client.app.state.store.get_return(return_id) == before

        def applied():
            current = client.get(root + "/jobs").json()[0]
            assert current["browserAvailable"], current
            if current["actionPending"]:
                return None
            assert current["actionError"] is None, current
            return current

        wait_until(applied)
        saved = client.get(root).json()
        field = next(item for item in saved["fields"] if item["id"] == field_id)
        native = client.get(root + "/browser").json()["values"][field_id]
        expected_native = json.loads(value) if field["kind"] == "multi" else value
        assert native == expected_native
        assert (field["value"] or "") == value
        assert field["valueOrigin"] == "preparer"
        assert field["citations"] == []
        assert not field["conflict"]

    body = {"expectedRevision": saved["revision"], "fieldId": "email", "value": "bad-email"}
    assert client.post(f"{root}/jobs/{job['id']}/field", json=body).status_code == 422
    body.update(value="another@example.test", expectedRevision=saved["revision"] - 1)
    assert client.post(f"{root}/jobs/{job['id']}/field", json=body).status_code == 409
    assert client.get(root + "/browser").json()["values"]["email"] == "reviewed@example.test"

    response = client.post(
        f"{root}/jobs/{job['id']}/submit", json={"expectedRevision": saved["revision"]}
    )
    assert response.status_code == 202, response.text
    wait_until(lambda: not client.get(root + "/jobs").json()[0]["actionPending"])
    assert not client.get(root + "/browser").json()["submitted"]
    assert requests == []


def test_stale_field_ack_does_not_save_or_later_promote_unconfirmed_value(workspace, monkeypatch):
    client, requests = workspace
    document = extract(client, monkeypatch, [group("Avery Morgan", 2025, "Engineer")])
    return_id = document["groups"][0]["returnId"]
    job = ready(client, return_id)
    store = client.app.state.store
    jobs = client.app.state.jobs
    before = store.get_return(return_id)

    with jobs.lock:
        jobs.apply_field(return_id, job["id"], before["revision"], "occupation", "Architect")
        # A competing persisted edit invalidates the pending command's revision.
        changed = store.correct(return_id, before["revision"], "notes", "Concurrent review", [])

    root = f"{RETURNS}/{return_id}"
    wait_until(lambda: not client.get(root + "/jobs").json()[0]["actionPending"])
    current = client.get(root + "/jobs").json()[0]
    assert current["actionError"]
    assert client.get(root + "/browser").json()["values"]["occupation"] == "Architect"

    # A later ordinary observation must not turn the failed apply into a saved correction.
    response = client.post(
        f"{root}/jobs/{job['id']}/interact", json={"kind": "focus", "fieldId": "email"}
    )
    assert response.status_code == 202, response.text
    wait_until(lambda: not client.get(root + "/jobs").json()[0]["actionPending"])
    saved = store.get_return(return_id)
    occupation = next(item for item in saved["fields"] if item["id"] == "occupation")
    assert occupation["value"] == "Engineer" and occupation["valueOrigin"] == "ai"
    assert saved["revision"] == changed["revision"]
    assert requests == []


def test_normalized_birth_date_shows_literal_document_citation_in_review(workspace, monkeypatch, tmp_path):
    client, requests = workspace
    document = extract(
        client,
        monkeypatch,
        [group("Avery Morgan", 2025, "Engineer")],
        content=b"Client: Avery Morgan\nTax year: 2025\nDate of birth: February 12, 1985\n",
    )
    return_id = document["groups"][0]["returnId"]
    ready(client, return_id)
    detail = client.get(f"{RETURNS}/{return_id}").json()
    origin = str(client.base_url).rstrip("/")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        page.goto(f"{origin}/clients/{detail['clientId']}/returns/2025")
        page.locator("#field-date_of_birth").click()
        expect(page.locator(".text-evidence-line.is-cited")).to_contain_text(
            "Date of birth: February 12, 1985"
        )
        expect(page.locator("#field-date_of_birth")).to_have_value("1985-02-12")
        expect(page.get_by_text("Evidence for Date of birth", exact=True)).to_be_visible()
        address = page.locator("#field-street_address")
        address.fill("42 Reviewed Lane")
        expect(page.locator("#field-navigation")).to_have_value("street_address")
        expect(address).to_be_focused()
        expect(page.locator("#source-panel-heading").locator("../..").locator("..")).to_contain_text(
            "No citation available for this field"
        )
        expect(page.locator(".text-evidence-line.is-cited")).to_have_count(0)
        address.press("Tab")
        expect(page.locator("#field-navigation")).to_have_value("street_address")
        page.locator("#field-date_of_birth").click()
        expect(page.locator(".text-evidence-line.is-cited")).to_contain_text(
            "Date of birth: February 12, 1985"
        )
        expect(address).to_have_value("42 Reviewed Lane")
        preview = page.locator("#actual-browser-viewport")
        preview.locator("img").wait_for(state="visible")
        assert preview.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        page.get_by_label("Preview zoom", exact=True).select_option("100")
        page.wait_for_function("""() => {
            const preview = document.querySelector('#actual-browser-viewport');
            return preview.scrollWidth > preview.clientWidth;
        }""")
        page.get_by_label("Preview zoom", exact=True).select_option("fit")
        page.wait_for_function("""() => {
            const preview = document.querySelector('#actual-browser-viewport');
            return preview.scrollWidth <= preview.clientWidth + 1;
        }""")
        preview.scroll_into_view_if_needed()
        image = tmp_path / "birth-date-citation.png"
        page.screenshot(path=str(image), full_page=False)
        print(f"Birth date citation screenshot: {image}")
        browser.close()
    assert requests == []
