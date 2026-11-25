"""Observe real input focus in a fresh human-review browser against the practice app."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from playwright.async_api import Page, async_playwright

from test_preparation_web_navigation import IDENTITY, chromium_executable
from src.sigma_demo.native import practice_web_profile
from src.sigma_demo.target import sigma_practice_target
from src.preparation.web_observer import ReviewSection, Selection, WebSelectionObserver


async def _create_draft(page: Page, origin: str, client_reference: str) -> str:
    await page.goto(origin)
    await page.get_by_label("Client reference").fill(client_reference)
    await page.get_by_label("Tax year").fill(str(IDENTITY.tax_year))
    await page.get_by_role("button", name="Search").click()
    await page.get_by_role("button", name="Create draft").click()
    await page.get_by_role("link", name="Add W-2").wait_for()
    return urlsplit(page.url).path.split("/")[2]


async def _add_w2(page: Page, employer: str) -> str:
    await page.get_by_role("link", name="Add W-2").click()
    await page.get_by_label("Employer reference").fill(employer)
    await page.get_by_label("Form instance").fill("1")
    await page.get_by_label("Box 1 wages").fill("100")
    await page.get_by_role("button", name="Save W-2").click()
    href = await page.get_by_role("row").filter(has_text=employer).get_by_role("link").get_attribute("href")
    assert href is not None
    return href.rsplit("/", 1)[-1]


def test_observes_row_scoped_click_tab_history_and_close_without_network_callbacks(tmp_path):
    executable = chromium_executable()

    async def exercise(origin: str) -> None:
        profile = practice_web_profile(origin)
        async with async_playwright() as playwright:
            # Seed through the native UI, close the writer, and give the preparer a distinct browser.
            seed = await playwright.chromium.launch(executable_path=executable)
            try:
                page = await seed.new_page()
                draft = await _create_draft(page, origin, IDENTITY.client_reference)
                record_a = await _add_w2(page, "SYNTHETIC EMPLOYER A")
                record_b = await _add_w2(page, "SYNTHETIC EMPLOYER B")
                other = await _create_draft(page, origin, "TEST-OTHER-PERSON")
            finally:
                await seed.close()

            browser = await playwright.chromium.launch(executable_path=executable)
            try:
                page = await browser.new_page()
                path = f"/drafts/{draft}/w2"
                await page.goto(profile.origin + path)
                requests = []
                page.on("request", lambda request: requests.append((request.method, request.url)))
                changes: asyncio.Queue[Selection | None] = asyncio.Queue()

                async def next_change() -> Selection | None:
                    return await asyncio.wait_for(changes.get(), 5)

                async def expect(selection: Selection | None) -> None:
                    async with asyncio.timeout(5):
                        while await changes.get() != selection:
                            pass

                section = ReviewSection("w2", (
                    ('input[name="wages"]', "wages"), ('input[name="withholding"]', "withholding"),
                ), active_record="form[data-record-id]")
                async with WebSelectionObserver(page, profile, IDENTITY, draft, (section,),
                                                r"[a-f0-9]{32}", changes.put_nowait):
                    await expect((draft, "w2", None, None))
                    await page.get_by_role("row").filter(has_text="SYNTHETIC EMPLOYER A").get_by_role("link").click()
                    await expect((draft, "w2", record_a, None))
                    await page.get_by_label("Box 1 wages").click()
                    await page.keyboard.press("Tab")
                    assert await next_change() == (draft, "w2", record_a, "wages")
                    assert await next_change() == (draft, "w2", record_a, "withholding")
                    await page.get_by_label("Employer reference").click()
                    assert await next_change() == (draft, "w2", record_a, None)
                    assert requests == [("GET", f"{profile.origin}{path}/{record_a}")]

                    await page.get_by_role("link", name="Back to W-2s").click()
                    await expect((draft, "w2", None, None))
                    await page.get_by_role("row").filter(has_text="SYNTHETIC EMPLOYER B").get_by_role("link").click()
                    await expect((draft, "w2", record_b, None))
                    await page.get_by_label("Box 1 wages").click()
                    assert await next_change() == (draft, "w2", record_b, "wages")

                    await page.go_back()
                    await expect((draft, "w2", None, None))
                    await page.go_forward()
                    # Chromium may restore the old input or focus the document; select it if needed.
                    while (restored := await next_change()) is None:
                        pass
                    assert restored in ((draft, "w2", record_b, None), (draft, "w2", record_b, "wages"))
                    if restored[-1] is None:
                        await page.get_by_label("Box 1 wages").click()
                        await expect((draft, "w2", record_b, "wages"))

                    await page.goto(f"{profile.origin}/drafts/{other}/w2")
                    assert await next_change() is None
                    await page.goto(f"{profile.origin}{path}/{record_a}")
                    await expect((draft, "w2", record_a, None))
                    await page.get_by_label("Box 1 wages").click()
                    assert await next_change() == (draft, "w2", record_a, "wages")
                    await page.close()
                    assert await next_change() is None
                assert all(method == "GET" and urlsplit(url).path in {
                    path, f"{path}/{record_a}", f"{path}/{record_b}", f"/drafts/{other}/w2",
                } and url.startswith(profile.origin + "/") for method, url in requests)
            finally:
                await browser.close()

    with sigma_practice_target(tmp_path / "drafts.sqlite3") as origin:
        asyncio.run(exercise(origin))


def test_reused_input_follows_profile_record_changes_without_refocus(tmp_path):
    executable = chromium_executable()
    draft, record_a, record_b = ("a" * 32, "b" * 32, "c" * 32)

    async def exercise(origin: str) -> None:
        profile = practice_web_profile(origin)
        path = f"/drafts/{draft}/w2"
        # An isolated synthetic SPA variant: its own UI switches records without moving focus.
        html = f"""
            <!doctype html>
            <main data-section="w2" data-draft-id="{draft}" data-status="unfiled"
                  data-client-reference="{IDENTITY.client_reference}" data-tax-year="{IDENTITY.tax_year}"
                  data-return-type="{IDENTITY.return_type}">
                <span id="a" data-record-id="{record_a}" aria-current="true"></span>
                <span id="b" data-record-id="{record_b}"></span>
                <label>Box 1 wages <input name="wages"></label>
                <button id="switch">Next record</button>
                <button id="away">Unknown view</button>
            </main>
            <script>
              const select = document.querySelector('#switch');
              const away = document.querySelector('#away');
              for (const button of [select, away])
                button.addEventListener('mousedown', event => event.preventDefault());
              select.addEventListener('click', () => {{
                document.querySelector('#a').removeAttribute('aria-current');
                document.querySelector('#b').setAttribute('aria-current', 'true');
              }});
              away.addEventListener('click', () => history.pushState(null, '', '/unknown'));
            </script>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=executable)
            try:
                page = await browser.new_page()
                await page.route(profile.origin + path, lambda route: route.fulfill(body=html, content_type="text/html"))
                await page.goto(profile.origin + path)
                changes: asyncio.Queue[Selection | None] = asyncio.Queue()

                async def next_change() -> Selection | None:
                    return await asyncio.wait_for(changes.get(), 5)

                section = ReviewSection("w2", (('input[name="wages"]', "wages"),),
                                        active_record='[aria-current="true"]', record_attributes=("aria-current",))
                async with WebSelectionObserver(page, profile, IDENTITY, draft, (section,),
                                                r"[a-f0-9]{32}", changes.put_nowait):
                    first = await next_change()
                    assert (await next_change() if first is None else first) == (draft, "w2", record_a, None)
                    await page.get_by_label("Box 1 wages").click()
                    assert await next_change() == (draft, "w2", record_a, "wages")
                    await page.get_by_role("button", name="Next record").click()
                    assert await next_change() == (draft, "w2", record_b, "wages")
                    assert await page.get_by_label("Box 1 wages").evaluate("(input) => input === document.activeElement")
                    await page.get_by_role("button", name="Unknown view").click()
                    assert await next_change() is None
                    await page.go_back()
                    assert await next_change() == (draft, "w2", record_b, "wages")
                assert await next_change() is None
            finally:
                await browser.close()

    with sigma_practice_target(tmp_path / "drafts.sqlite3") as origin:
        asyncio.run(exercise(origin))
