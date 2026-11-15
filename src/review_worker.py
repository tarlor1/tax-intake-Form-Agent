"""Isolated Chromium control of the unchanged external intake placeholder.

Only normal browser controls write the target. The supervisor alone writes local
observations/provenance; neither controller credentials nor HTTP API helpers enter
this process.
"""

from __future__ import annotations

import ctypes
import os
import signal
import threading
import time
from urllib.parse import urlsplit

from .intake_contract import field_definitions
from .review_target import fillable_values, form_payload

# Full-page JPEGs retain every native control; cap transport size without cropping.
MAX_BROWSER_FRAME_BYTES = 2 * 1024 * 1024
BROWSER_VIEWPORT = {"width": 1100, "height": 800}
ALLOWED_KEYS = frozenset(
    {
        "Backspace",
        "Delete",
        "Tab",
        "Shift+Tab",
        "Enter",
        "Escape",
        "Space",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "Control+A",
        "Control+C",
        "Control+X",
        "Control+Z",
        "Control+Y",
        "Control+Shift+Z",
        "Control+ArrowLeft",
        "Control+ArrowRight",
        "Control+ArrowUp",
        "Control+ArrowDown",
        "Control+Home",
        "Control+End",
        "Control+Shift+Home",
        "Control+Shift+End",
        "Shift+ArrowLeft",
        "Shift+ArrowRight",
        "Shift+ArrowUp",
        "Shift+ArrowDown",
        "Shift+Home",
        "Shift+End",
        "Control+Shift+ArrowLeft",
        "Control+Shift+ArrowRight",
        "Control+Shift+ArrowUp",
        "Control+Shift+ArrowDown",
        "Meta+A",
        "Meta+C",
        "Meta+X",
        "Meta+Z",
        "Meta+Y",
        "Meta+Shift+Z",
        "Meta+ArrowLeft",
        "Meta+ArrowRight",
        "Meta+ArrowUp",
        "Meta+ArrowDown",
        "Meta+Home",
        "Meta+End",
        "Meta+Shift+Home",
        "Meta+Shift+End",
        "Meta+Shift+ArrowLeft",
        "Meta+Shift+ArrowRight",
        "Meta+Shift+ArrowUp",
        "Meta+Shift+ArrowDown",
        "Shift+PageUp",
        "Shift+PageDown",
    }
)


class WorkerFailure(Exception):
    """Data-free actionable browser failure."""


class WorkerCancelled(Exception):
    pass


def local_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/", "/index.html"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        return parsed.scheme, parsed.hostname, parsed.port or 80
    except (TypeError, ValueError):
        raise ValueError(
            "Workers require a plain HTTP loopback intake URL without credentials or query."
        ) from None


def allowed_worker_url(url: str, target_url: str, method: str = "GET") -> bool:
    """Only the original page and its native submission endpoint are reachable."""
    try:
        parsed = urlsplit(url)
        return (
            (parsed.scheme, parsed.hostname, parsed.port or 80) == local_origin(target_url)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (
                (method == "GET" and parsed.path in {"/", "/index.html", "/favicon.ico"})
                or (method == "POST" and parsed.path == "/api/intake")
            )
        )
    except (TypeError, ValueError):
        return False


def _take_process_ownership(parent_pid: int, job_id: str) -> None:
    os.setsid()
    os.environ["REVIEW_BROWSER_JOB"] = job_id

    def terminate(_signum, _frame):
        raise WorkerCancelled()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise RuntimeError("Could not establish worker parent-death supervision.")
    if os.getppid() != parent_pid:
        raise WorkerCancelled()

    def parent_watchdog() -> None:
        while os.getppid() == parent_pid:
            time.sleep(0.25)
        time.sleep(2)
        os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=parent_watchdog, name="intake-owner-watchdog", daemon=True).start()


# Read-only DOM inspection: no installed listeners, target globals or value writes.
_OBSERVE = """() => {
    const form = document.querySelector('#intake-form');
    if (!form) throw new Error('Original intake form unavailable');
    const values = {income_sources: [], deductions: []};
    for (const [name,value] of new FormData(form).entries()) {
        if (Array.isArray(values[name])) values[name].push(value);
        else values[name] = value;
    }
    const active = document.activeElement;
    const focusedFieldId = active && form.contains(active) ? active.name || null : null;
    const invalid = [...form.elements].filter(el => el.willValidate && !el.validity.valid);
    const confirmation = document.querySelector('#confirmation');
    const intakeId = document.querySelector('#intake-id')?.textContent.trim() || null;
    const submitted = !!confirmation && !confirmation.hidden && !!intakeId;
    const error = document.querySelector('#form-error');
    return {values, focusedFieldId, submitted, intakeId: submitted ? intakeId : null,
        invalidFieldIds: invalid.map(el => el.name),
        issues: submitted ? [] : invalid.map(el =>
            (el.labels?.[0]?.textContent.trim() || el.name) + ': ' + el.validationMessage)
            .concat(error && !error.hidden ? [error.textContent.trim()] : [])};
}"""


def run_worker(
    snapshot: dict,
    target_url: str,
    job_id: str,
    messages,
    cancellation,
    headless: bool,
    parent_pid: int,
) -> None:
    """Duplex messages carry fixed commands and ACKs, never JavaScript or URLs."""
    playwright = browser = context = None
    outcome = {"kind": "failed", "error": "Browser closed before handoff."}
    definitions = {item["id"]: item for item in field_definitions()}
    last_observation = None
    last_frame = 0.0

    def check_cancelled() -> None:
        if cancellation.is_set():
            raise WorkerCancelled()

    def send_stage(text: str) -> None:
        check_cancelled()
        messages.send({"kind": "stage", "stage": text})

    def frame() -> None:
        nonlocal last_frame
        check_cancelled()
        jpeg = page.screenshot(
            type="jpeg", quality=55, full_page=True, animations="allow", timeout=5000
        )
        if len(jpeg) > MAX_BROWSER_FRAME_BYTES:
            jpeg = page.screenshot(
                type="jpeg", quality=25, full_page=True, animations="allow", timeout=5000
            )
        if len(jpeg) > MAX_BROWSER_FRAME_BYTES:
            raise WorkerFailure("Actual full browser page exceeded the preview size limit.")
        messages.send({"kind": "frame", "jpeg": jpeg})
        last_frame = time.monotonic()

    def observe(commit_focused: bool = False) -> dict:
        state = page.evaluate(_OBSERVE)
        focused = state["focusedFieldId"]
        state["commitFields"] = [
            field_id
            for field_id, definition in definitions.items()
            if commit_focused
            or field_id != focused
            or definition.get("kind") in {"multi", "select"}
        ]
        return state

    def fill(field_id: str, value: str | list[str]) -> None:
        definition = definitions[field_id]
        controls = page.locator(f'[name="{field_id}"]')
        if definition.get("kind") == "multi":
            for control in controls.all():
                control.set_checked(control.get_attribute("value") in value)
        elif definition.get("kind") == "select":
            controls.select_option(value)
        else:
            controls.fill(value)

    try:
        _take_process_ownership(parent_pid, job_id)
        local_origin(target_url)
        if urlsplit(target_url).path != "/index.html":
            raise WorkerFailure("The independent target URL must end in /index.html.")

        expected = form_payload(snapshot)
        available, _ = fillable_values(snapshot)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise WorkerFailure(
                "Install the review dependencies and run python -m playwright install chromium."
            ) from exc

        send_stage("Launching independent intake browser")
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless, timeout=20000)
        context = browser.new_context(
            accept_downloads=False, service_workers="block", viewport=BROWSER_VIEWPORT
        )
        context.set_default_timeout(10000)
        context.set_default_navigation_timeout(15000)

        def restrict_request(route) -> None:
            request = route.request
            if (
                not cancellation.is_set()
                and request.redirected_from is None
                and allowed_worker_url(request.url, target_url, request.method)
            ):
                route.continue_()
            else:
                route.abort("blockedbyclient")

        context.route("**/*", restrict_request)

        page = context.new_page()
        context.on("page", lambda other: other.close() if other is not page else None)
        send_stage("Opening original intake form")
        response = page.goto(target_url, wait_until="domcontentloaded")
        if response is None or response.status != 200 or page.url != target_url:
            raise WorkerFailure("The independent original intake page could not be opened.")
        frame()

        filled = 0
        for field_id, value in expected.items():
            check_cancelled()
            fill(field_id, value)
            if field_id in available:
                filled += 1
            messages.send(
                {
                    "kind": "stage",
                    "stage": f"Filled {filled} of {len(available)} available fields",
                    "completedFields": filled,
                }
            )
            if field_id in available and cancellation.wait(0.15):
                raise WorkerCancelled()
            if time.monotonic() - last_frame >= 1.0:
                frame()

        # Move focus without invoking submit, preserving all native validation.
        page.locator('[name="full_name"]').focus()
        state = observe()
        if state["values"] != expected:
            raise WorkerFailure(
                "Real form readback differs from the grounded values; preparation stopped."
            )
        frame()
        messages.send({"kind": "handoff", **state})
        last_observation = state
        send_stage("Filled and observed; review the fields and browser preview before submitting")

        while True:
            check_cancelled()
            if page.is_closed() or not browser.is_connected():
                raise WorkerFailure(
                    "The actual browser was closed; local observations are not a submission receipt."
                )
            if messages.poll():
                command = messages.recv()
                command_id = command.get("id")
                action = command.get("action", {})
                try:
                    kind = action["kind"]
                    if kind == "click":
                        page.mouse.click(action["x"], action["y"])
                    elif kind == "wheel":
                        page.mouse.move(action["x"], action["y"])
                        page.mouse.wheel(0, action["deltaY"])
                    elif kind == "key":
                        page.keyboard.press(action["key"])
                    elif kind == "text":
                        page.keyboard.insert_text(action["text"])
                    elif kind == "focus":
                        page.locator(f'[name="{action["fieldId"]}"]').first.focus()
                    elif kind == "source":
                        fill(action["fieldId"], action["value"])
                        page.locator(f'[name="{action["fieldId"]}"]').first.focus()
                    elif kind == "field":
                        field_id = action["fieldId"]
                        fill(field_id, action["value"])
                        # Native blur/change handlers finish before whole-field readback.
                        next_field = "email" if field_id == "full_name" else "full_name"
                        page.locator(f'[name="{next_field}"]').focus()
                    elif kind == "submit":
                        page.get_by_role("button", name="Submit intake", exact=True).click()
                    else:
                        raise WorkerFailure("Unsupported browser command.")

                    # Let native change/submit handlers run. Never synthesize target integration calls.
                    page.wait_for_timeout(80)
                    state = observe(
                        commit_focused=kind in {"field", "submit"}
                        or (kind == "key" and action["key"] == "Enter")
                    )
                    if (
                        kind in {"source", "field"}
                        and state["values"].get(action["fieldId"]) != action["value"]
                    ):
                        raise WorkerFailure(
                            "Requested value did not match actual control readback."
                        )
                    messages.send(
                        {"kind": "ack", "id": command_id, "observation": state, "error": None}
                    )
                    last_observation = state
                except WorkerCancelled:
                    raise
                except Exception:
                    try:
                        state = observe()
                        last_observation = state
                    except Exception:
                        state = None
                    messages.send(
                        {
                            "kind": "ack",
                            "id": command_id,
                            "observation": state,
                            "error": "The actual browser could not apply this action. Inspect the form and retry.",
                        }
                    )

            state = observe()
            if state != last_observation:
                messages.send({"kind": "observation", **state})
                last_observation = state
            if time.monotonic() - last_frame >= 1.0:
                frame()
            page.wait_for_timeout(100)
    except WorkerCancelled:
        outcome = {"kind": "cancelled"}
    except WorkerFailure as exc:
        outcome = {"kind": "failed", "error": str(exc)}
    except BaseException as exc:
        if cancellation.is_set():
            outcome = {"kind": "cancelled"}
        elif "Executable doesn't exist" in str(exc):
            outcome = {
                "kind": "failed",
                "error": "Chromium could not launch. Run python -m playwright install chromium.",
            }
        else:
            outcome = {
                "kind": "failed",
                "error": "Actual browser work failed. Check Chromium setup and the independent intake server.",
            }
    finally:
        for resource in (context, browser):
            if resource is not None:
                try:
                    resource.close()
                except BaseException:
                    pass
        if playwright is not None:
            try:
                playwright.stop()
            except BaseException:
                pass
        try:
            messages.send(outcome)
        except (BrokenPipeError, EOFError, OSError):
            pass
        messages.close()
