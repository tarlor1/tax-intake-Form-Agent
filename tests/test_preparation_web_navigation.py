"""Exercise the guarded browser against the independent synthetic draft target."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import which
from threading import Thread
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Error
from pydantic import TypeAdapter, ValidationError

from src.preparation.native_plan import DraftIdentity
from src.preparation.sigma_web import CreatePermit, SearchResult, WebBlocked, WebDraft, WebProfile
from src.sigma_demo.native import practice_web_profile, practice_web_settings as _settings
from src.sigma_demo.target import EXACT_SEARCH_COVERAGE, sigma_practice_target


IDENTITY = DraftIdentity(client_reference="TEST-PERSON-01", tax_year=2025, return_type="individual")
_OTHER_KEY = "a" * 32


def chromium_executable():
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    for pattern in ("chromium_headless_shell-*/chrome*/chrome-headless-shell",
                    "chromium_headless_shell-*/chrome*/headless_shell", "chromium-*/chrome*/chrome"):
        if executable := next((item for item in sorted(cache.glob(pattern), reverse=True) if item.is_file()), None):
            return str(executable.resolve())
    if executable := which("chromium") or which("chromium-browser"):
        return executable
    pytest.skip("Install Chromium with python -m playwright install chromium")


def _permit(profile, absence, intent="test-journal-intent"):
    return CreatePermit(profile.profile_id, absence.identity, absence.proof, intent)


def test_explicit_exact_loopback_profile_opt_in():
    for origin in ("http://127.0.0.1:8123", "https://127.0.0.1:8123", "http://example.invalid"):
        with pytest.raises(ValueError, match="origin"):
            WebProfile(**_settings(origin))
    for origin in ("https://example.invalid", "http://127.0.0.1:8123/", "http://127.0.0.1:8123/?x=1",
                   "http://user@127.0.0.1:8123", "http://127.1:8123"):
        with pytest.raises(ValueError, match="origin"):
            WebProfile.for_test(**_settings(origin))


def test_visible_worker_cannot_open_a_nonpractice_profile():
    async def exercise():
        profile = WebProfile(**_settings("https://example.invalid"))
        with pytest.raises(ValueError, match="loopback practice profile"):
            async with WebDraft.launch(profile, headed_practice=True):
                pytest.fail("A nonpractice browser must not be launched")

    asyncio.run(exercise())


def test_approved_connector_profile_round_trips_without_accepting_extra_settings():
    adapter = TypeAdapter(WebProfile)
    profile = WebProfile(**_settings("https://example.invalid"))
    serialized = adapter.dump_json(profile, exclude={"_draft_re"})
    assert adapter.validate_json(serialized) == profile
    with pytest.raises(ValidationError) as rejected:
        adapter.validate_python({**_settings(profile.origin), "credentials": "not-a-profile-setting"})
    assert rejected.value.errors()[0]["loc"] == ("credentials",)


def test_search_create_and_read_only_reopen_in_fresh_browser(tmp_path):
    executable = chromium_executable()

    async def exercise(profile):
        async with WebDraft.launch(profile, executable_path=executable) as session:
            absent = await session.search(IDENTITY)
            assert (absent.status, absent.draft_key) == ("absent", None)
            with pytest.raises(WebBlocked, match="draft-not-found"):
                await session.open_existing(IDENTITY)

            journal = []
            posts = []
            session.page.on("request", lambda request: posts.append(tuple(journal)) if request.method == "POST" else None)

            async def creation_journal(proof: SearchResult) -> CreatePermit:
                assert proof.status == "absent" and proof.identity == IDENTITY and proof.draft_key is None
                assert proof.proof != absent.proof
                assert await session.page.get_by_role("status").inner_text() == "No matching draft."
                assert posts == []
                journal.append(proof.proof)
                return _permit(profile, proof)

            key = await session.create(IDENTITY, creation_journal)
            assert posts == [tuple(journal)] and len(journal) == 1
            assert await session.require_draft(IDENTITY, key) == ("w2", None)
            with pytest.raises(WebBlocked, match="create-already-dispatched-or-draft-bound"):
                await session.create(IDENTITY, creation_journal)

        async with WebDraft.launch(profile, executable_path=executable) as fresh:
            requested = []
            fresh.page.on("request", lambda request: requested.append((request.method, urlsplit(request.url).path)))
            found = await fresh.search(IDENTITY)
            assert (found.status, found.draft_key) == ("found", key)
            with pytest.raises(WebBlocked, match="durable-draft-key-mismatch"):
                await fresh.open_existing(IDENTITY, _OTHER_KEY)
            assert all(path == "/" for _, path in requested)
            requested.clear()
            assert await fresh.open_existing(IDENTITY) == key
            assert requested == [("GET", "/"), ("GET", "/"), ("GET", f"/drafts/{key}/w2")]
            requested.clear()
            assert await fresh.reopen(IDENTITY, key) == key
            assert requested == [("GET", "/"), ("GET", "/"), ("GET", f"/drafts/{key}/w2")]
            with pytest.raises(WebBlocked, match="different-return-already-bound"):
                await fresh.require_draft(IDENTITY.model_copy(update={"tax_year": 2024}), key)

    with sigma_practice_target(tmp_path / "drafts.sqlite3") as origin:
        asyncio.run(exercise(practice_web_profile(origin)))


def test_wrong_permit_and_unarmed_native_create_cannot_create(tmp_path):
    executable = chromium_executable()

    async def exercise(profile):
        async with WebDraft.launch(profile, executable_path=executable) as session:
            async def wrong_proof(absence):
                return CreatePermit(profile.profile_id, absence.identity, "unrelated-proof", "test-intent")

            with pytest.raises(WebBlocked, match="creation-intent-not-authorized"):
                await session.create(IDENTITY, wrong_proof)
            assert (await session.search(IDENTITY)).status == "absent"
            with suppress(Error):
                await session.page.get_by_role("button", name="Create draft").click()
            with pytest.raises(WebBlocked, match="unexpected-browser"):
                await session.search(IDENTITY)

        async with WebDraft.launch(profile, executable_path=executable) as fresh:
            assert (await fresh.search(IDENTITY)).status == "absent"

    with sigma_practice_target(tmp_path / "drafts.sqlite3") as origin:
        asyncio.run(exercise(practice_web_profile(origin)))


def test_creation_refreshes_absence_after_intent_before_post(tmp_path):
    executable = chromium_executable()

    async def exercise(profile):
        async with WebDraft.launch(profile, executable_path=executable) as session:
            other_key = None
            posts = []
            session.page.on("request", lambda request: posts.append(request.method) if request.method == "POST" else None)

            async def creation_journal(absence):
                nonlocal other_key
                async with WebDraft.launch(profile, executable_path=executable) as human:
                    async def other_authority(other_absence):
                        return _permit(profile, other_absence, "other-intent")

                    other_key = await human.create(IDENTITY, other_authority)
                return _permit(profile, absence)

            with pytest.raises(WebBlocked, match="draft-no-longer-absent"):
                await session.create(IDENTITY, creation_journal)
            assert posts == [] and other_key
            assert await session.open_existing(IDENTITY, other_key) == other_key

    with sigma_practice_target(tmp_path / "drafts.sqlite3") as origin:
        asyncio.run(exercise(practice_web_profile(origin)))


def test_save_rejects_unreviewed_native_payload_before_write(tmp_path):
    executable = chromium_executable()

    async def exercise(profile):
        async def authorize(absence):
            return _permit(profile, absence)

        async def rideshare(session, key):
            path = f"/drafts/{key}/rideshare"
            link = session.page.get_by_role("link", name="Rideshare", exact=True)
            with pytest.raises(WebBlocked, match="draft-link-outside-requested-scope"):
                await session.follow(IDENTITY, key, link, path=f"/drafts/{key}/transmit")
            await session.follow(IDENTITY, key, link, path=path)
            assert await session.require_draft(IDENTITY, key) == ("rideshare", None)
            return path

        async with WebDraft.launch(profile, executable_path=executable) as session:
            key = await session.create(IDENTITY, authorize)
            path = await rideshare(session, key)
            await session.page.get_by_label("Gross receipts", exact=True).fill("999")
            form = session.page.locator("form")
            save = form.get_by_role("button", name="Save rideshare", exact=True)
            with pytest.raises(WebBlocked, match="unexpected-browser-request"):
                await session.submit(
                    IDENTITY, key, form, save, path=path, result_path=path,
                    expected_fields=(("gross_receipts", "6200"), ("platform_fees", "")),
                )

        async with WebDraft.launch(profile, executable_path=executable) as fresh:
            await fresh.reopen(IDENTITY, key)
            await rideshare(fresh, key)
            assert await fresh.page.get_by_label("Gross receipts", exact=True).input_value() == ""

    with sigma_practice_target(tmp_path / "drafts.sqlite3") as origin:
        asyncio.run(exercise(practice_web_profile(origin)))


@contextmanager
def _serve(reply):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            seen.append((self.command, self.path))
            status, headers, body = reply(self.command, self.path)
            content = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(content)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", seen
        finally:
            server.shutdown()
            thread.join()


_SEARCH_FORM = '''<form action="/" method="get" aria-label="Exact draft search">
<label>Client reference <input name="client_reference"></label>
<label>Tax year <input name="tax_year"></label>
<label>Return type <select name="return_type"><option value="individual">Individual</option></select></label>
<button>Search</button></form>'''
_MATCH = f'''<section aria-label="Exact search result" data-draft-id="{_OTHER_KEY}"
 data-client-reference="TEST-PERSON-01" data-tax-year="2025" data-return-type="individual" data-status="unfiled">
<a href="/drafts/{_OTHER_KEY}/w2">Open draft</a></section>'''
_ABSENT = '''<p role="status">No matching draft.</p><form action="/drafts" method="post">
<input type="hidden" name="client_reference" value="TEST-PERSON-01">
<input type="hidden" name="tax_year" value="2025"><input type="hidden" name="return_type" value="individual">
<button>Create draft</button></form>'''


def _list(result=""):
    return f"<!doctype html><main><h1>Return list</h1>{_SEARCH_FORM}{result}</main>"


@pytest.mark.parametrize(("coverage", "result"), [
    ("", ""),
    ("", _ABSENT),
    (EXACT_SEARCH_COVERAGE.replace("exact-exhaustive", "current-page"), _ABSENT),
    (EXACT_SEARCH_COVERAGE.replace("Exact search complete.", "Search unavailable for other offices."), _ABSENT),
    (EXACT_SEARCH_COVERAGE, _ABSENT + '<p role="alert">Only one office was searched.</p>'),
    (EXACT_SEARCH_COVERAGE, _ABSENT + '<p hidden>Search unavailable for archived returns.</p>'),
    (EXACT_SEARCH_COVERAGE, _ABSENT + '<nav><a href="/?page=2" rel="next">Next page</a></nav>'),
    (EXACT_SEARCH_COVERAGE, _MATCH + _MATCH),
    (EXACT_SEARCH_COVERAGE, _MATCH.replace('data-client-reference="TEST-PERSON-01"', 'data-client-reference="TEST-OTHER"')),
    (EXACT_SEARCH_COVERAGE, _MATCH.replace('data-tax-year="2025"', 'data-tax-year="2024"')),
    (EXACT_SEARCH_COVERAGE, _MATCH.replace('data-return-type="individual"', 'data-return-type="business"')),
    (EXACT_SEARCH_COVERAGE, _MATCH.replace('data-status="unfiled"', 'data-status="filed"')),
    (EXACT_SEARCH_COVERAGE, _ABSENT.replace('value="2025"', 'value="2024"')),
    (EXACT_SEARCH_COVERAGE, _ABSENT.replace("<button>", '<button formtarget="_blank">')),
], ids=["incomplete", "status-without-coverage", "partial-coverage", "marker-warning", "warning", "unreviewed-notice", "pagination",
        "ambiguous", "wrong-client", "wrong-year", "wrong-type", "filed", "unsafe-create", "popup-override"])
def test_incomplete_or_wrong_search_never_authorizes_create(coverage, result):
    executable = chromium_executable()

    async def exercise(profile):
        async with WebDraft.launch(profile, executable_path=executable) as session:
            async def must_not_authorize(_):
                pytest.fail("unsafe search reached the creation journal")

            with pytest.raises(WebBlocked):
                await session.create(IDENTITY, must_not_authorize)

    with _serve(lambda _, path: (200, {}, _list(coverage + result if urlsplit(path).query else ""))) as (origin, seen):
        asyncio.run(exercise(practice_web_profile(origin)))
        assert [(method, urlsplit(path).path) for method, path in seen] == [("GET", "/"), ("GET", "/")]


@pytest.mark.parametrize("outside_origin", [False, True], ids=["different-path", "different-origin"])
def test_off_allowlist_redirect_is_blocked_before_network(outside_origin):
    executable = chromium_executable()

    async def exercise(profile):
        async with WebDraft.launch(profile, executable_path=executable) as session:
            with pytest.raises(WebBlocked, match="unexpected-browser"):
                await session.search(IDENTITY)

    with _serve(lambda *_: (200, {}, "not an approved origin")) as (other, other_seen):
        destination = f"{other}/" if outside_origin else "/unexpected"
        with _serve(lambda *_: (302, {"Location": destination}, "")) as (origin, seen):
            asyncio.run(exercise(practice_web_profile(origin)))
            assert seen == [("GET", "/")]
        assert other_seen == []


def test_redirect_chain_after_create_stops_before_unapproved_target():
    executable = chromium_executable()
    landing = f"/drafts/{_OTHER_KEY}/w2"

    def reply(method, path):
        if method == "POST":
            return 303, {"Location": landing}, ""
        if path == landing:
            return 302, {"Location": "/transmit"}, ""
        return 200, {}, _list(EXACT_SEARCH_COVERAGE + _ABSENT if urlsplit(path).query else "")

    async def exercise(profile):
        async with WebDraft.launch(profile, executable_path=executable) as session:
            async def authorize(absence):
                return _permit(profile, absence)

            with pytest.raises(WebBlocked, match="unexpected-browser"):
                await session.create(IDENTITY, authorize)
            with pytest.raises(WebBlocked, match="create-already-dispatched-or-draft-bound"):
                await session.create(IDENTITY, authorize)

    with _serve(reply) as (origin, seen):
        asyncio.run(exercise(practice_web_profile(origin)))
        assert seen[-2:] == [("POST", "/drafts"), ("GET", landing)]
        assert [path for method, path in seen if method == "POST"] == ["/drafts"]


def test_child_frame_cannot_request_even_allowed_path():
    executable = chromium_executable()

    async def exercise(profile):
        async with WebDraft.launch(profile, executable_path=executable) as session:
            with pytest.raises(WebBlocked, match="unexpected-(child-frame|browser)"):
                await session.search(IDENTITY)

    with _serve(lambda *_: (200, {}, _list('<iframe src="/"></iframe>'))) as (origin, seen):
        asyncio.run(exercise(practice_web_profile(origin)))
        assert seen == [("GET", "/")]
