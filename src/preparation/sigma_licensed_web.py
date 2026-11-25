"""Read-only qualification of the licensed Sigma Web 2025 return entry point."""

from __future__ import annotations

import argparse
import asyncio
import re
import stat
import sys
from getpass import getpass
from pathlib import Path

from playwright.async_api import Error, Page, Request, Route, async_playwright

from .sigma_web import WebBlocked

_ORIGIN = "https://stp.cloudtaxoffice.com"
_DISPLAY = re.compile(rf"{re.escape(_ORIGIN)}/proavalon/TaxpayerProfile/Display\?_=[0-9]{{1,20}}")
_ENTRY = "Start New 2025 Tax Return"
_DIALOG = "dialog[open]:visible, [role=dialog]:visible, .modal.show:visible"
_SSN_SEGMENTS = (3, 2, 4, 3, 2, 4)


def _allow_display(method: str, url: str) -> bool:
    return method == "GET" and _DISPLAY.fullmatch(url) is not None


async def inspect_return_entry(page: Page, *, mock_ssn: str | None = None) -> None:
    """Verify the 2025 modal and optionally test mock entry; never submit it."""
    if mock_ssn is not None and not re.fullmatch(r"987-?65-?432[0-9]", mock_ssn):
        raise WebBlocked("Use a mock SSN from the reserved 987-65-4320 through 987-65-4329 range.")

    context = page.context
    if context.pages != [page] or not page.url.startswith(f"{_ORIGIN}/"):
        raise WebBlocked("Sigma inspection requires one owned page on the approved origin.")

    candidate = page.get_by_role("button", name=_ENTRY, exact=True).and_(page.locator("a[role=button]"))
    try:
        await candidate.wait_for(timeout=120_000)
    except Error:
        raise WebBlocked("Sigma 2025 entry was not visible. Check sign-in and the selected year.") from None
    if await page.locator(_DIALOG).count() or (
        await candidate.get_attribute("href") not in {"javascript:void(0)", "javascript:void(0);", "javascript:;", "#", ""}
        or await candidate.get_attribute("target") is not None
        or await candidate.locator("xpath=ancestor::form").count()
    ):
        raise WebBlocked("The Sigma 2025 entry control does not match the reviewed dialog launcher.")

    async def guard(route: Route, request: Request) -> None:
        try:
            if request.frame == page.main_frame and request.resource_type == "xhr" and _allow_display(
                request.method, request.url,
            ):
                await route.continue_()
                return
        except Error:
            await route.abort()
            return
        await route.abort()

    # The context guard includes popups. Chromium's Fetch guard checks redirects
    # too, because Playwright's context route runs only for the first hop.
    cdp = await context.new_cdp_session(page)
    frame_id = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
    display_started = False

    async def guard_hop(event: dict) -> None:
        nonlocal display_started
        request = event["request"]
        if (
            not display_started and event.get("frameId") == frame_id and event.get("resourceType") == "XHR"
            and _allow_display(request["method"], request["url"])
        ):
            display_started = True
            await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        else:
            await cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})

    await context.route("**/*", guard)
    cdp.on("Fetch.requestPaused", guard_hop)
    await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})

    try:
        await candidate.focus(timeout=5_000)
        async with page.expect_response(lambda response: _allow_display(response.request.method, response.url)) as loaded:
            await candidate.press("Enter", timeout=5_000)
        if not (await loaded.value).ok or not display_started:
            raise WebBlocked("Sigma did not load the reviewed SSN dialog.")
        await page.locator(_DIALOG).first.wait_for(timeout=5_000)
        await page.locator(_DIALOG).locator("input:visible").first.wait_for(timeout=5_000)
        shape = await page.locator(_DIALOG).evaluate_all(r"""dialogs => dialogs.map(dialog => {
            const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
            return {
                ssn: /\bSSN\b|Social Security Number/i.test(dialog.innerText),
                start: Array.from(dialog.querySelectorAll("a"))
                    .filter(el => visible(el) && el.innerText.trim() === "START RETURN").length,
                fields: Array.from(dialog.querySelectorAll("input"))
                    .filter(visible).map(el => [el.type, el.maxLength]),
            };
        })""")
        expected = {"ssn": True, "start": 1, "fields": [["text", length] for length in _SSN_SEGMENTS]}
        if context.pages != [page] or not page.url.startswith(f"{_ORIGIN}/") or shape != [expected]:
            raise WebBlocked("Unexpected Sigma draft prompt context, action, or SSN field layout.")
        if mock_ssn is not None:
            digits = mock_ssn.replace("-", "")
            segments = [digits[:3], digits[3:5], digits[5:]]
            inputs = page.locator(_DIALOG).locator("input:visible")
            if await inputs.evaluate_all("fields => fields.some(field => field.value !== '')"):
                raise WebBlocked("The Sigma SSN dialog is not blank.")

            # Key events trigger Sigma's native focus advance between segments.
            await inputs.first.focus(timeout=5_000)
            await page.keyboard.type(digits * 2, delay=45)
            if await inputs.evaluate_all("fields => fields.map(field => field.value)") != segments * 2:
                raise WebBlocked("Sigma did not accept all six mock SSN segments. Nothing was submitted.")
    except Error:
        raise WebBlocked("Sigma did not show the reviewed 2025 SSN dialog.") from None


async def _inspect(profile_directory: Path, chromium: Path | None, mock_ssn: str | None = None) -> None:
    if (not profile_directory.is_absolute() or not profile_directory.is_dir() or profile_directory.is_symlink()
            or (sys.platform != "win32" and profile_directory.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))):
        raise WebBlocked("Use an absolute, private, dedicated Chromium profile directory.")
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            profile_directory, executable_path=chromium, headless=False, accept_downloads=False,
            chromium_sandbox=True, service_workers="block",
        )
        try:
            if len(context.pages) != 1 or context.pages[0].url != "about:blank":
                raise WebBlocked("The dedicated profile must start with one blank tab.")
            await context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.pages[0]
            await page.goto(_ORIGIN, wait_until="domcontentloaded")
            print("Waiting for the signed-in Sigma 2025 workspace. Sign in if prompted.", flush=True)
            await inspect_return_entry(page, mock_ssn=mock_ssn)
            if mock_ssn is None:
                print("Verified Sigma 2025 SSN prompt: 3-2-4 and 3-2-4. No values entered or submitted.")
            else:
                print("Verified all six mock SSN segments in Sigma. Start Return was not pressed.")
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-directory", required=True, type=Path)
    parser.add_argument("--chromium", type=Path, help="Path to the approved Chromium executable, if not bundled.")
    parser.add_argument("--check-mock-ssn", action="store_true", help="Prompt for a reserved mock SSN to test autofill.")
    args = parser.parse_args()
    try:
        mock_ssn = getpass("Reserved mock SSN: ") if args.check_mock_ssn else None
        asyncio.run(_inspect(args.profile_directory, args.chromium, mock_ssn))
    except WebBlocked as exc:
        parser.exit(1, f"{exc}\n")
    except Error:
        parser.exit(1, "Unable to open the dedicated Sigma browser. Check that the profile is not in use.\n")


if __name__ == "__main__":
    main()
