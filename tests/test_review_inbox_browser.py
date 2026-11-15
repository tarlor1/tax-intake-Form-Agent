"""Document drops, visible progress, and explicit extraction consent in the real UI."""

from __future__ import annotations

import re
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from starlette.concurrency import run_in_threadpool

from review_fixtures import source
from src.review_api import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH = {"X-Review-Token": "inbox-browser-test"}


@pytest.fixture
def inbox_server(tmp_path, monkeypatch):
    ui = PROJECT_ROOT / "review_ui" / "dist"
    if not (ui / "index.html").is_file():
        pytest.skip("Build review_ui before running the inbox browser regression")

    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key-never-sent")
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    upload_release = threading.Event()
    extraction_release = threading.Event()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        app = create_app(
            tmp_path / "workspace",
            ui,
            port=port,
            token=AUTH["X-Review-Token"],
        )

        @app.middleware("http")
        async def hold_upload(request, call_next):
            if request.method == "POST" and request.url.path == "/api/review/intake/documents":
                if not await run_in_threadpool(upload_release.wait, 15):
                    raise RuntimeError("Browser test did not release the upload")
            return await call_next(request)

        def fail_extraction(_pages):
            if not extraction_release.wait(15):
                raise RuntimeError("Browser test did not release extraction")
            raise RuntimeError("Synthetic extraction failure; no external request was sent.")

        # Replace only the external model boundary, never the upload or review application.
        monkeypatch.setattr(app.state.intake, "_call_gemini", fail_extraction)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
        )
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()

        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    pytest.fail("Isolated inbox server did not start")
                threading.Event().wait(0.02)

            yield app, f"http://127.0.0.1:{port}", upload_release, extraction_release
        finally:
            upload_release.set()
            extraction_release.set()
            server.should_exit = True
            thread.join(timeout=20)
            assert not thread.is_alive(), "Isolated inbox server did not stop"


@pytest.mark.parametrize("drop_target", [".document-dropzone strong", "h1"])
def test_pdf_and_text_drops_show_progress_and_require_consent(
    inbox_server, tmp_path, monkeypatch, drop_target,
):
    app, origin, upload_release, extraction_release = inbox_server
    _, pdf, _ = source("inbox-contact", "Synthetic contact", ["Client: Jordan Example", "Tax year: 2025"])
    files = []
    for filename, content in (
        ("contact.pdf", pdf),
        ("contact.txt", b"Client: Jordan Example\nTax year: 2025\n"),
        ("unsupported.jpg", b"image"),
    ):
        path = tmp_path / filename
        path.write_bytes(content)
        files.append(str(path))

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
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(origin + "/clients")
        readiness = page.locator("#extraction-readiness")
        extract = page.get_by_role("button", name="Extract uploaded documents", exact=True)
        expect(readiness).to_contain_text("No files are waiting")
        expect(extract).to_be_disabled()

        prevented = page.locator("header").first.evaluate(
            """element => {
                const transfer = new DataTransfer();
                transfer.items.add(new File(['outside'], 'outside.txt', { type: 'text/plain' }));
                const event = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer });
                element.dispatchEvent(event);
                return event.defaultPrevented;
            }"""
        )
        assert prevented, "Dropping outside the inbox must not navigate to the file"

        target = page.locator(drop_target).bounding_box()
        assert target is not None
        drag = {
            "x": target["x"] + target["width"] / 2,
            "y": target["y"] + target["height"] / 2,
            "data": {"items": [], "files": files, "dragOperationsMask": 1},
        }
        cdp = page.context.new_cdp_session(page)
        for event_type in ("dragEnter", "dragOver", "drop"):
            cdp.send("Input.dispatchDragEvent", {"type": event_type, **drag})
        upload_progress = page.get_by_role("progressbar", name="Uploading and validating documents")
        expect(upload_progress).to_be_visible()
        assert upload_progress.get_attribute("aria-valuenow") is None
        expect(readiness).to_contain_text("finish uploading")
        expect(extract).to_be_disabled()

        upload_release.set()
        expect(page.get_by_text("Saved locally · extraction needed", exact=True)).to_have_count(2)
        expect(upload_progress).to_have_count(0)
        expect(readiness).to_contain_text("Check the approval box")
        expect(extract).to_be_disabled()
        rejected = page.locator("li").filter(has=page.get_by_text("unsupported.jpg", exact=True))
        expect(rejected.get_by_role("alert")).to_be_visible()
        saved = page.request.get(origin + "/api/review/intake", headers=AUTH).json()
        assert {document["filename"] for document in saved["documents"]} == {"contact.pdf", "contact.txt"}
        assert all(document["status"] == "uploaded" for document in saved["documents"])
        assert app.state.store.list_returns() == []
        assert page.url == origin + "/clients"

        page.get_by_role("checkbox", name=re.compile("I approve sending")).check()
        expect(extract).to_be_enabled()
        extract.click()
        extraction_progress = page.get_by_role("progressbar", name="Extracting uploaded documents")
        expect(extraction_progress).to_be_visible()
        assert extraction_progress.get_attribute("aria-valuenow") is None
        desktop_image = tmp_path / "inbox-desktop.png"
        page.screenshot(path=str(desktop_image), full_page=True)
        print(f"Inbox desktop screenshot: {desktop_image}")

        extraction_release.set()
        expect(page.get_by_text("Extraction failed", exact=True)).to_have_count(2, timeout=15000)
        expect(extraction_progress).to_have_count(0)
        expect(page.get_by_role("button", name="Retry extraction", exact=True)).to_have_count(2)
        for filename in ("contact.pdf", "contact.txt"):
            failed_file = page.locator("li").filter(has=page.get_by_text(filename, exact=True))
            expect(failed_file.get_by_role("alert")).to_be_visible()

        monkeypatch.setenv("GEMINI_API_KEY", "")
        expect(readiness).to_contain_text("Set GEMINI_API_KEY", timeout=10000)
        expect(extract).to_be_disabled()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        mobile_image = tmp_path / "inbox-mobile.png"
        page.screenshot(path=str(mobile_image), full_page=True)
        print(f"Inbox mobile screenshot: {mobile_image}")
        assert page_errors == []
        assert page.evaluate("window.__cspViolations") == []
        browser.close()


def test_expired_session_stops_polling_and_reload_restores_upload(inbox_server, tmp_path):
    app, origin, upload_release, _ = inbox_server
    stale_requests = []

    def send_stale_token(route):
        stale_requests.append(route.request.url)
        route.continue_(headers={**route.request.headers, "x-review-token": "previous-server-session"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.clock.install()
        page.goto(origin + "/clients")
        expect(page.locator("#extraction-readiness")).to_contain_text("No files are waiting")

        # The real backend must reject this old-session header; no response is mocked.
        page.route("**/api/review/intake", send_stale_token)
        expect(page.get_by_role("alert")).to_contain_text("Session expired", timeout=10000)
        expect(page.get_by_role("button", name="Select files", exact=True)).to_have_count(0)
        rejected_count = len(stale_requests)
        assert rejected_count == 1
        page.clock.fast_forward(10000)
        assert len(stale_requests) == rejected_count

        image = tmp_path / "expired-session.png"
        page.screenshot(path=str(image), full_page=True)
        print(f"Expired session screenshot: {image}")

        page.unroute("**/api/review/intake", send_stale_token)
        page.get_by_role("button", name="Reload workspace", exact=True).click()
        expect(page.locator("#extraction-readiness")).to_contain_text("No files are waiting")
        expect(page.get_by_role("heading", name="Clients", exact=True)).to_be_visible()
        expect(page.get_by_role("region", name="Document inbox", exact=True)).to_be_visible()

        upload_release.set()
        page.get_by_label("Select PDF or TXT documents").set_input_files({
            "name": "after-reload.txt",
            "mimeType": "text/plain",
            "buffer": b"Client: Jordan Example\nTax year: 2025\n",
        })
        expect(page.get_by_text("Saved locally · extraction needed", exact=True)).to_be_visible()
        saved = page.request.get(origin + "/api/review/intake", headers=AUTH).json()
        assert {document["filename"] for document in saved["documents"]} == {"after-reload.txt"}
        assert app.state.store.list_returns() == []
        browser.close()
