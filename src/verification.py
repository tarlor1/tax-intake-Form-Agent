"""Step 5: confirm a submission actually landed, in the browser and in the intake system.

PASS requires all of: the confirmation panel is shown, the intake ID has the expected format, and
the intake system holds a record under that intake ID with the values that were submitted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, expect

from .form_filler import form_values
from .models import IntakeExtraction

log = logging.getLogger(__name__)

INTAKE_ID_RE = re.compile(r"^TAX-\d{8}-[0-9A-F]{4}$")
CONFIRMATION_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    intake_id: str | None
    detail: str
    screenshot: Path | None


def verify_submission(
    page: Page,
    record: IntakeExtraction,
    lookup_submission: Callable[[str], dict[str, Any] | None],
    screenshot_dir: Path,
) -> VerificationResult:
    confirmation = page.locator("#confirmation")
    receipt = confirmation.locator("#intake-id")
    try:
        expect(confirmation).to_be_visible(timeout=CONFIRMATION_TIMEOUT_MS)
        expect(confirmation).to_contain_text("Intake received")
        intake_id = receipt.inner_text().strip()
    except (AssertionError, PlaywrightError):
        return _failed("No readable intake confirmation was shown")

    if not INTAKE_ID_RE.fullmatch(intake_id):
        return _failed("Intake ID does not match the expected format")
    stored = lookup_submission(intake_id)
    if stored is None:
        return _failed("Intake ID was not found in the local intake store")
    mismatched = [name for name, value in form_values(record).items() if not _same(stored.get(name), value)]
    if mismatched:
        return _failed(f"Intake system recorded different values for: {', '.join(mismatched)}")

    detail = f"Confirmation shown; intake {intake_id} recorded with matching details"
    screenshot = _take_screenshot(receipt, screenshot_dir, intake_id)
    log.info("Verification PASS: %s", detail)
    return VerificationResult(passed=True, intake_id=intake_id, detail=detail, screenshot=screenshot)


def _same(stored: Any, submitted: str | list[str]) -> bool:
    """Checkbox groups are compared as sets: the order the browser sends them in does not matter."""
    if isinstance(submitted, list):
        return isinstance(stored, list) and sorted(stored) == sorted(submitted)
    return stored == submitted


def _failed(detail: str) -> VerificationResult:
    # The editable form can still contain raw identity values, so never capture this screen.
    log.error("Verification FAIL: %s", detail)
    return VerificationResult(passed=False, intake_id=None, detail=detail, screenshot=None)


def _take_screenshot(receipt: Locator, folder: Path, name: str) -> Path | None:
    path = folder / f"{name}.png"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # The rest of the confirmation displays client contact and identity details.
        receipt.screenshot(path=str(path))
        return path
    except (OSError, PlaywrightError):
        log.warning("Could not save confirmation screenshot %s", path.name)
        return None
