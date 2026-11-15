"""Real original-form DOM interaction, native validation, receipts and ownership.

No target HTML replacement, fake worker completion, extraction calls or direct
submission helpers. The test may inspect the stand-in's receipt after a human
browser command activates its real Submit intake button.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from review_fixtures import source
from src.form_server import IntakeServer, _IntakeHandler
from src.review_jobs import ReviewJobs
from src.review_store import ReviewError, ReviewStore
from src.review_target import form_payload
from src.review_worker import MAX_BROWSER_FRAME_BYTES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FULL_VALUES = {
    "email": "avery@example.test",
    "phone": "555-0100",
    "date_of_birth": "1985-02-12",
    "marital_status": "single",
    "preferred_contact_method": "email",
    "street_address": "14 Example Lane",
    "city": "Portland",
    "state": "OR",
    "zip_code": "97201",
    "drivers_license_number": "SYNTHETIC-1234",
    "drivers_license_state": "OR",
    "drivers_license_issued_on": "2020-04-10",
    "drivers_license_expires_on": "2030-04-10",
    "filing_status": "single",
    "number_of_dependents": "0",
    "occupation": "Designer",
    "employer": "Example Harbor Labs",
    "income_sources": '["w2_wages","rental"]',
    "deductions": '["charitable","education"]',
    "urgency": "medium",
    "deadline_date": "2026-12-01",
    "irs_notice_received": "no",
    "consultation_requested": "yes",
    "request_summary": "Prepare the documented intake",
    "notes": "Human review required",
}


@pytest.fixture
def browser_workspace(tmp_path, monkeypatch):
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Install Chromium with python -m playwright install chromium")
    requests = []
    original_get, original_post = _IntakeHandler.do_GET, _IntakeHandler.do_POST

    def record_get(handler):
        requests.append(("GET", handler.path, dict(handler.headers)))
        return original_get(handler)

    def record_post(handler):
        requests.append(("POST", handler.path, dict(handler.headers)))
        return original_post(handler)

    monkeypatch.setattr(_IntakeHandler, "do_GET", record_get)
    monkeypatch.setattr(_IntakeHandler, "do_POST", record_post)
    with IntakeServer(PROJECT_ROOT / "intake_form") as target:
        store = ReviewStore(tmp_path / "data")
        queue = ReviewJobs(store, target_url=target.url + "index.html", max_workers=1)
        try:
            yield store, queue, target, requests
        finally:
            queue.close()


def new_detail(store, *, values=None, name="Avery Morgan"):
    detail = store.create_return(2025, client_name=name, return_type="individual")
    for field_id, value in (FULL_VALUES if values is None else values).items():
        detail = store.correct(detail["id"], detail["revision"], field_id, value, [])
    return detail


def wait_until(check, *, timeout=60):
    pause = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        pause.wait(0.04)
    pytest.fail(
        "Real browser did not reach the required observable state before its regression deadline."
    )


def current_job(queue, return_id, job_id):
    return next(item for item in queue.list_jobs(return_id) if item["id"] == job_id)


def open_browser(queue, detail):
    job = queue.enqueue(detail["id"], detail["revision"])
    queue.start()

    def ready():
        state = current_job(queue, detail["id"], job["id"])
        assert state["status"] not in {"failed", "interrupted", "cancelled"}, state
        return (
            state if state["status"] == "ready_for_review" and state["browserAvailable"] else None
        )

    return wait_until(ready)


def action(queue, detail, job, payload):
    enqueued = queue.interact(detail["id"], job["id"], payload)
    assert enqueued["actionPending"]

    def acknowledged():
        state = current_job(queue, detail["id"], job["id"])
        assert state["browserAvailable"], state
        if not state["actionPending"]:
            assert state["actionError"] is None, state
            return state
        return None

    return wait_until(acknowledged)


def submit_as_human(queue, detail, job):
    # Native keyboard activation emits a real click on the existing target button.
    # No automation ever submits; these are explicit preparer interaction commands.
    action(queue, detail, job, {"kind": "focus", "fieldId": "notes"})
    action(queue, detail, job, {"kind": "key", "key": "Tab"})
    action(queue, detail, job, {"kind": "key", "key": "Space"})


def test_full_real_form_handoff_then_human_native_submit_observes_actual_receipt(browser_workspace):
    store, queue, target, requests = browser_workspace
    detail = new_detail(store)
    job = open_browser(queue, detail)
    state = queue.browser_state(detail["id"])
    assert state["values"] == form_payload(detail)
    assert state["issues"] == []
    assert not state["submitted"] and state["intakeId"] is None
    assert job["completedFields"] == job["totalFields"]
    assert store.get_return(detail["id"]) == detail
    assert not any(method == "POST" for method, _, _ in requests)
    jpeg = queue.browser_frame(detail["id"], job["id"])
    assert jpeg and jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    assert len(jpeg) <= MAX_BROWSER_FRAME_BYTES
    submit_as_human(queue, detail, job)
    submitted = wait_until(
        lambda: (
            queue.browser_state(detail["id"])
            if queue.browser_state(detail["id"])["submitted"]
            else None
        )
    )
    receipt = target.get_submission(submitted["intakeId"])
    assert receipt is not None
    # The actual unchanged target owns the submitted payload and receipt.
    assert {key: receipt[key] for key in form_payload(detail)} == form_payload(detail)
    assert submitted["browserAvailable"]
    assert [path for method, path, _ in requests if method == "POST"] == ["/api/intake"]
    assert all(
        not any(
            name.lower() in {"x-review-token", "x-practice-job", "authorization", "cookie"}
            for name in headers
        )
        for _, _, headers in requests
    )
    assert all(path in {"/index.html", "/favicon.ico", "/api/intake"} for _, path, _ in requests)
    with sqlite3.connect(store.database_path) as connection:
        observed = connection.execute(
            "SELECT intake_id,values_json FROM review_browser_receipts WHERE job_id=?", (job["id"],)
        ).fetchone()
    assert observed[0] == submitted["intakeId"]
    assert json.loads(observed[1]) == form_payload(detail)


def test_partial_required_data_remains_editable_and_native_validation_prevents_submit(
    browser_workspace,
):
    store, queue, target, requests = browser_workspace
    values = {key: value for key, value in FULL_VALUES.items() if key not in {"email", "urgency"}}
    detail = new_detail(store, values=values)
    job = open_browser(queue, detail)
    state = queue.browser_state(detail["id"])
    assert state["values"]["email"] == "" and state["values"]["urgency"] == ""
    assert state["issues"] and state["browserAvailable"]
    submit_as_human(queue, detail, job)
    assert not queue.browser_state(detail["id"])["submitted"]
    assert not any(method == "POST" for method, _, _ in requests)
    action(queue, detail, job, {"kind": "focus", "fieldId": "email"})
    action(queue, detail, job, {"kind": "text", "text": "unfinished@"})
    assert (
        next(item for item in store.get_return(detail["id"])["fields"] if item["id"] == "email")[
            "value"
        ]
        is None
    )
    action(queue, detail, job, {"kind": "key", "key": "Tab"})
    assert queue.browser_state(detail["id"])["values"]["email"] == "unfinished@"
    assert (
        next(item for item in store.get_return(detail["id"])["fields"] if item["id"] == "email")[
            "value"
        ]
        is None
    )
    action(queue, detail, job, {"kind": "focus", "fieldId": "email"})
    action(queue, detail, job, {"kind": "key", "key": "Control+A"})
    action(
        queue,
        detail,
        job,
        {"kind": "text", "text": "completed@example.test", "reason": "Confirmed with client"},
    )
    action(queue, detail, job, {"kind": "key", "key": "Tab"})
    action(queue, detail, job, {"kind": "focus", "fieldId": "urgency"})
    action(queue, detail, job, {"kind": "key", "key": "ArrowDown"})
    action(queue, detail, job, {"kind": "key", "key": "Tab"})
    changed = store.get_return(detail["id"])
    email = next(item for item in changed["fields"] if item["id"] == "email")
    assert email["value"] == "completed@example.test" and email["valueOrigin"] == "preparer"
    assert email["citations"] == [] and email["correctionReason"] == "Confirmed with client"
    assert queue.browser_state(detail["id"])["values"]["urgency"] == "low"
    submit_as_human(queue, detail, job)
    submitted = wait_until(
        lambda: (
            queue.browser_state(detail["id"])
            if queue.browser_state(detail["id"])["submitted"]
            else None
        )
    )
    receipt = target.get_submission(submitted["intakeId"])
    assert receipt["email"] == "completed@example.test"
    assert receipt["urgency"] == "low"


def test_real_checkbox_select_text_fifo_and_commit_provenance(browser_workspace):
    store, queue, _, _ = browser_workspace
    detail = new_detail(store)
    job = open_browser(queue, detail)
    action(queue, detail, job, {"kind": "focus", "fieldId": "income_sources"})
    action(queue, detail, job, {"kind": "key", "key": "Space"})
    assert queue.browser_state(detail["id"])["values"]["income_sources"] == ["rental"]
    action(queue, detail, job, {"kind": "key", "key": "Tab"})
    action(queue, detail, job, {"kind": "key", "key": "Space"})
    assert queue.browser_state(detail["id"])["values"]["income_sources"] == [
        "self_employment",
        "rental",
    ]
    action(queue, detail, job, {"kind": "focus", "fieldId": "filing_status"})
    action(queue, detail, job, {"kind": "key", "key": "ArrowDown"})
    action(queue, detail, job, {"kind": "key", "key": "Tab"})
    assert queue.browser_state(detail["id"])["values"]["filing_status"] == "married_filing_jointly"
    action(queue, detail, job, {"kind": "focus", "fieldId": "notes"})
    action(queue, detail, job, {"kind": "key", "key": "Control+A"})
    before = store.get_return(detail["id"])["revision"]
    # Accept multiple pending human keystrokes, preserve order, and only commit on blur.
    with queue.lock:
        for payload in (
            {"kind": "text", "text": "First"},
            {"kind": "text", "text": " second"},
            {"kind": "key", "key": "Tab"},
        ):
            assert queue.interact(detail["id"], job["id"], payload)["actionPending"]
    wait_until(lambda: not current_job(queue, detail["id"], job["id"])["actionPending"])
    assert current_job(queue, detail["id"], job["id"])["actionError"] is None
    changed = store.get_return(detail["id"])
    assert queue.browser_state(detail["id"])["values"]["notes"] == "First second"
    notes = next(item for item in changed["fields"] if item["id"] == "notes")
    assert notes["value"] == "First second" and notes["valueOrigin"] == "preparer"
    assert notes["citations"] == []
    assert changed["revision"] == before + 1
    action(queue, detail, job, {"kind": "wheel", "x": 500, "y": 400, "deltaY": -500})
    assert store.get_return(detail["id"])["revision"] == changed["revision"]


def test_source_choice_changes_real_dom_then_records_source_not_preparer(browser_workspace):
    store, queue, _, _ = browser_workspace
    detail = new_detail(store)
    _, pdf, citations = source(
        "phones", "Client phone evidence", ["Phone: 555-0101", "Correct phone: 555-0199"]
    )
    detail = store.add_document(detail["id"], detail["revision"], "phones.pdf", pdf)
    document_id = detail["documents"][-1]["id"]
    for citation, value in zip(citations, ("555-0101", "555-0199")):
        detail = store.add_proposal(
            detail["id"], detail["revision"], "phone", document_id, 1, citation["rect"], value
        )
    phone = next(item for item in detail["fields"] if item["id"] == "phone")
    chosen = phone["candidates"][-1]
    job = open_browser(queue, detail)
    before_events = len(detail["events"])
    pending = queue.choose_source(
        detail["id"],
        job["id"],
        detail["revision"],
        "phone",
        chosen["id"],
        reason="Verified corrected phone",
    )
    assert pending["actionPending"]
    wait_until(lambda: not current_job(queue, detail["id"], job["id"])["actionPending"])
    assert current_job(queue, detail["id"], job["id"])["actionError"] is None
    changed = store.get_return(detail["id"])
    phone = next(item for item in changed["fields"] if item["id"] == "phone")
    assert queue.browser_state(detail["id"])["values"]["phone"] == chosen["value"] == phone["value"]
    assert phone["valueOrigin"] == "source" and phone["selectedCandidateId"] == chosen["id"]
    assert phone["citations"] == chosen["citations"]
    assert not any(
        event["kind"] == "manual_correction" for event in changed["events"][before_events:]
    )
    assert any(
        "Verified corrected phone" in event["message"]
        for event in changed["events"][before_events:]
    )
    stable_revision = changed["revision"]
    action(queue, detail, job, {"kind": "focus", "fieldId": "city"})
    assert store.get_return(detail["id"])["revision"] == stable_revision


def test_retained_handoff_releases_automation_slot_and_requires_explicit_close(browser_workspace):
    store, queue, _, _ = browser_workspace
    first = new_detail(store)
    first_job = open_browser(queue, first)
    second = new_detail(store, name="Second Client")
    second_job = open_browser(queue, second)
    assert current_job(queue, first["id"], first_job["id"])["browserAvailable"]
    assert current_job(queue, second["id"], second_job["id"])["browserAvailable"]
    with pytest.raises(ReviewError) as error:
        queue.enqueue(first["id"], store.get_return(first["id"])["revision"])
    assert error.value.status_code == 409
    running = queue._running[first_job["id"]]
    worker_pid = running.pid
    queue.cancel(first["id"], first_job["id"])
    wait_until(lambda: current_job(queue, first["id"], first_job["id"])["status"] == "cancelled")
    assert not queue.browser_state(first["id"])["browserAvailable"]
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)
    replacement = open_browser(queue, store.get_return(first["id"]))
    assert replacement["id"] != first_job["id"]
    assert queue.browser_frame(first["id"], first_job["id"]) is None
    with pytest.raises(ReviewError):
        queue.interact(first["id"], first_job["id"], {"kind": "text", "text": "old worker"})
    assert current_job(queue, second["id"], second_job["id"])["browserAvailable"]


def test_shutdown_reaps_retained_browser_and_restart_keeps_only_honest_observations(
    browser_workspace,
):
    store, queue, target, _ = browser_workspace
    detail = new_detail(store)
    job = open_browser(queue, detail)
    pid = queue._running[job["id"]].pid
    observed = queue.browser_state(detail["id"])
    queue.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    reopened = ReviewJobs(store, target_url=target.url + "index.html")
    try:
        state = reopened.browser_state(detail["id"])
        assert (
            state["values"] == observed["values"] and state["observedAt"] == observed["observedAt"]
        )
        assert not state["submitted"] and not state["browserAvailable"]
        assert reopened.browser_frame(detail["id"], job["id"]) is None
        assert reopened._running == {}
    finally:
        reopened.close()


def test_review_ui_applies_local_edits_and_scrolls_view_only_preview(
    tmp_path,
    monkeypatch,
):
    import socket

    import uvicorn

    from src.review_api import create_app

    ui = PROJECT_ROOT / "review_ui" / "dist"
    if not (ui / "index.html").is_file():
        pytest.skip("Build review_ui before running the browser UI regression")

    monkeypatch.setenv("GEMINI_API_KEY", "")

    with IntakeServer(PROJECT_ROOT / "intake_form") as target, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(
            tmp_path / "ui-workspace",
            ui,
            port=port,
            target_url=target.url + "index.html",
        )
        detail = new_detail(app.state.store, name="Casey Example")
        detail = app.state.store.correct(
            detail["id"],
            detail["revision"],
            "email",
            "before@example.test",
            [],
        )

        _, pdf, citations = source(
            "legacy-contact",
            "Client contact evidence",
            ["Email: before@example.test"],
        )
        detail = app.state.store.add_document(
            detail["id"],
            detail["revision"],
            "contact.pdf",
            pdf,
        )
        pdf_document_id = detail["documents"][-1]["id"]
        detail = app.state.store.add_document(
            detail["id"],
            detail["revision"],
            "contact.txt",
            b"TXT source for Casey Example\nEmail: before@example.test",
            "text/plain",
        )
        text_document_id = detail["documents"][-1]["id"]
        email = next(item for item in detail["fields"] if item["id"] == "email")
        detail["legacyFields"] = [
            {
                **email,
                "id": "historical-email",
                "label": "Historical email",
                "valueOrigin": "source",
                "citations": [{**citations[0], "documentId": pdf_document_id}],
            }
        ]
        with sqlite3.connect(app.state.store.database_path) as connection:
            connection.execute(
                "UPDATE review_returns SET payload = ? WHERE id = ?",
                (json.dumps(detail), detail["id"]),
            )

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                proxy_headers=False,
            )
        )
        thread = threading.Thread(
            target=lambda: server.run(sockets=[listener]),
            daemon=True,
        )
        thread.start()

        try:
            wait_until(lambda: server.started)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1100})
                page.add_init_script("""
                    window.__cspViolations = [];
                    document.addEventListener('securitypolicyviolation', event => {
                        window.__cspViolations.push({
                            directive: event.effectiveDirective,
                            blockedURI: event.blockedURI,
                        });
                    });
                """)
                page.goto(
                    f"http://127.0.0.1:{port}/clients/{detail['clientId']}/returns/2025",
                )
                page.get_by_role("button", name="Open and fill form", exact=True).click()
                preview = page.locator("#actual-browser-viewport")
                image = preview.locator("img")
                image.wait_for(state="visible", timeout=30000)

                changes = []
                page.on(
                    "request",
                    lambda request: changes.append(request.url)
                    if request.method == "POST" and "/jobs/" in request.url else None,
                )
                page.locator("#field-navigation").select_option("email")
                email_input = page.locator("#field-email")
                with page.expect_response(lambda response: response.url.endswith("/browser")):
                    email_input.fill("after@example.test")
                assert email_input.input_value() == "after@example.test"
                assert not any(url.endswith("/field") for url in changes)
                wait_until(lambda: app.state.jobs.browser_state(detail["id"])["browserAvailable"])
                assert app.state.jobs.browser_state(detail["id"])["values"]["email"] == "before@example.test"

                page.get_by_role("button", name=f"Apply {email['label']}", exact=True).click()
                wait_until(
                    lambda: app.state.jobs.browser_state(detail["id"])["values"].get("email")
                    == "after@example.test",
                )
                def recorded_email():
                    saved = app.state.store.get_return(detail["id"])
                    email_field = next(item for item in saved["fields"] if item["id"] == "email")
                    return saved if email_field["value"] == "after@example.test" else None

                saved = wait_until(recorded_email)
                saved_email = next(item for item in saved["fields"] if item["id"] == "email")
                assert saved_email["valueOrigin"] == "preparer"
                assert saved_email["citations"] == []
                page.get_by_role(
                    "group", name=email["label"], exact=True
                ).get_by_text("Confirmed from browser", exact=True).wait_for()
                assert len([url for url in changes if url.endswith("/field")]) == 1

                definitions = {item["id"]: item for item in detail["fields"]}
                date_input = page.locator("#field-date_of_birth")
                assert date_input.get_attribute("type") == "date"
                date_input.fill("1987-03-14")
                page.get_by_role(
                    "button", name=f"Apply {definitions['date_of_birth']['label']}", exact=True
                ).click()
                page.get_by_role(
                    "group", name=definitions["date_of_birth"]["label"], exact=True
                ).get_by_text("Confirmed from browser", exact=True).wait_for()
                assert app.state.jobs.browser_state(detail["id"])["values"]["date_of_birth"] == "1987-03-14"

                page.locator("#field-state").select_option("CA")
                page.get_by_role(
                    "button", name=f"Apply {definitions['state']['label']}", exact=True
                ).click()
                page.get_by_role(
                    "group", name=definitions["state"]["label"], exact=True
                ).get_by_text("Confirmed from browser", exact=True).wait_for()
                assert app.state.jobs.browser_state(detail["id"])["values"]["state"] == "CA"

                page.locator("#field-income_sources-rental").uncheck()
                page.get_by_role(
                    "button", name=f"Apply {definitions['income_sources']['label']}", exact=True
                ).click()
                page.get_by_role(
                    "group", name=definitions["income_sources"]["label"], exact=True
                ).get_by_text("Confirmed from browser", exact=True).wait_for()
                assert app.state.jobs.browser_state(detail["id"])["values"]["income_sources"] == ["w2_wages"]

                page.get_by_label("Preview zoom", exact=True).select_option("100")
                preview.scroll_into_view_if_needed()
                page.wait_for_function(
                    """() => {
                        const viewport = document.querySelector('#actual-browser-viewport');
                        const image = viewport.querySelector('img');
                        return image.naturalHeight > image.naturalWidth
                            && viewport.scrollHeight > viewport.clientHeight + 500;
                    }""",
                )
                before_scroll = list(changes)
                bounds = preview.bounding_box()
                page.evaluate("""
                    () => {
                        const viewport = document.querySelector('#actual-browser-viewport');
                        window.__previewScroll = {points: [], done: false};
                        let start;
                        function sample(time) {
                            start ??= time;
                            window.__previewScroll.points.push({time, top: viewport.scrollTop});
                            if (time - start < 2200) requestAnimationFrame(sample);
                            else window.__previewScroll.done = true;
                        }
                        requestAnimationFrame(sample);
                    }
                """)
                cdp = page.context.new_cdp_session(page)
                cdp.send(
                    "Input.synthesizeScrollGesture",
                    {
                        "x": bounds["x"] + bounds["width"] / 2,
                        "y": bounds["y"] + min(bounds["height"] / 2, 200),
                        "yDistance": -1000,
                        "speed": 600,
                        "gestureSourceType": "mouse",
                        "preventFling": True,
                    },
                )
                page.wait_for_function("() => window.__previewScroll.done")
                sample = page.evaluate("window.__previewScroll.points")
                moving = [
                    point for previous, point in zip(sample, sample[1:])
                    if point["top"] != previous["top"]
                ]
                assert len(moving) > 1
                intervals = [
                    current["time"] - previous["time"]
                    for previous, current in zip(moving, moving[1:])
                ]
                fps = 1000 * (len(moving) - 1) / (moving[-1]["time"] - moving[0]["time"])
                p95 = sorted(intervals)[int((len(intervals) - 1) * 0.95)]
                print(
                    f"Native preview scroll: {fps:.1f} position updates/s; "
                    f"p95 interval {p95:.1f} ms; max interval {max(intervals):.1f} ms"
                )
                assert changes == before_scroll
                scroll_top = preview.evaluate("element => element.scrollTop")
                page.wait_for_event(
                    "response", predicate=lambda response: response.url.endswith("/frame")
                )
                assert preview.evaluate("element => element.scrollTop") == scroll_top

                page.locator("#field-navigation").select_option("historical-email")
                page.wait_for_url("**?field=historical-email")
                page.wait_for_function(
                    """() => document.querySelector('.field-evidence h3')?.textContent
                        .startsWith('Historical email')""",
                )
                page.wait_for_selector(".pdf-paper", state="visible")
                assert page.locator(".observed-fact").inner_text() == "before@example.test"
                assert "before@example.test" in page.locator(
                    ".pdf-paper canvas",
                ).get_attribute("aria-label")

                page.locator("#source-document").select_option(text_document_id)
                page.wait_for_selector(".text-evidence-viewport .text-evidence-line")
                assert (
                    "TXT source for Casey Example"
                    in page.locator(
                        ".text-evidence-viewport",
                    ).inner_text()
                )
                assert page.evaluate("window.__cspViolations") == []
                page.get_by_role(
                    "checkbox", name="I have reviewed the original intake and want to submit it now."
                ).check()
                page.get_by_role("button", name="Submit original form", exact=True).click()
                wait_until(lambda: app.state.jobs.browser_state(detail["id"])["submitted"])
                assert app.state.jobs.browser_state(detail["id"])["intakeId"]
                image_path = tmp_path / "editable-review.png"
                page.screenshot(path=str(image_path), full_page=True)
                print(f"Editable review screenshot: {image_path}")
                browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)
