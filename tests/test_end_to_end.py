"""Fill the real intake form in headless Chromium and verify the submission.

Skipped when Playwright's Chromium is not installed (`playwright install chromium`).
"""

from __future__ import annotations

import json
from dataclasses import replace
from struct import unpack

import pytest
from playwright.sync_api import Error as PlaywrightError

import main as cli
from src import verification
from src.config import PROJECT_ROOT
from src.extraction import parse_extraction
from src.form_filler import BrowserSession, fill_intake_form
from src.form_server import IntakeServer
from src.verification import verify_submission

RECORD = {
    "full_name": "Test Client",
    "email": "test.client@example.com",
    "phone": "(555) 010-0000",
    "date_of_birth": "1990-01-31",
    "street_address": "100 Main St, Apt 2",
    "city": "Austin",
    "state": "TX",
    "zip_code": "78701",
    "drivers_license_number": "12345678",
    "drivers_license_state": "NY",
    "drivers_license_issued_on": "2022-01-31",
    "drivers_license_expires_on": "2030-01-31",
    "tax_year": 2025,
    "filing_status": "head_of_household",
    "number_of_dependents": 1,
    "income_sources": ["w2_wages", "unemployment"],
    "deductions": ["childcare", "marketplace_insurance"],
    "irs_notice_received": False,
    "deadline_date": "2026-11-16",
    "consultation_requested": True,
    "urgency": {"level": "medium", "reason": "Wants the return filed soon."},
    "missing_fields": [],
}


@pytest.fixture
def page():
    with BrowserSession(headless=True, slow_mo_ms=0) as browser:
        try:
            yield browser.page()
        except PlaywrightError as exc:  # pragma: no cover - depends on the machine
            pytest.skip(f"Chromium is not available: {exc}")


def test_form_is_filled_submitted_and_verified(page, tmp_path):
    record = parse_extraction(json.dumps(RECORD))
    with IntakeServer(PROJECT_ROOT / "intake_form") as server:
        fill_intake_form(page, server.url, record, notes="Test run.")
        result = verify_submission(page, record, server.get_submission, tmp_path)
        stored = server.get_submission(result.intake_id) if result.intake_id else None

    assert result.passed, result.detail
    assert stored["state"] == "TX" and stored["drivers_license_state"] == "NY"
    assert stored["income_sources"] == ["w2_wages", "unemployment"]
    assert stored["irs_notice_received"] == "no"
    assert result.screenshot is not None and result.screenshot.is_file()
    assert "12345678" not in page.locator("#confirmation").inner_text(), "the confirmation masks the license"
    # A successful screenshot covers only the verified receipt ID, not even the masked summary.
    receipt_box = page.locator("#confirmation #intake-id").bounding_box()
    assert receipt_box is not None
    width, height = unpack(">II", result.screenshot.read_bytes()[16:24])
    assert abs(width - receipt_box["width"]) <= 2
    assert abs(height - receipt_box["height"]) <= 2


def test_incomplete_record_is_never_submitted(page):
    record = parse_extraction(json.dumps({**RECORD, "drivers_license_number": None}))

    with pytest.raises(ValueError, match="incomplete"):
        fill_intake_form(page, "http://127.0.0.1:9/", record, notes="")


def test_verification_failure_never_captures_editable_form(page, tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "CONFIRMATION_TIMEOUT_MS", 100)
    page.set_content(
        '<form><input name="license" value="SYNTHETIC_PRIVATE_ID"></form>'
        '<section id="confirmation" hidden>Intake received</section>'
    )
    record = parse_extraction(json.dumps(RECORD))

    result = verify_submission(page, record, lambda _: pytest.fail("unverified ID was looked up"), tmp_path)

    assert not result.passed
    assert result.intake_id is None and result.screenshot is None
    assert "SYNTHETIC_PRIVATE_ID" not in result.detail
    assert not list(tmp_path.iterdir())


def test_visible_confirmation_without_stored_intake_is_not_exported(page, tmp_path):
    record = parse_extraction(json.dumps(RECORD))
    with IntakeServer(PROJECT_ROOT / "intake_form") as server:
        fill_intake_form(page, server.url, record, notes="Test run.")
        result = verify_submission(page, record, lambda _: None, tmp_path)

    assert not result.passed
    assert result.intake_id is None and result.screenshot is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("checklist_error", [True, False], ids=["generator-error", "empty-draft"])
def test_cli_returns_attention_with_verified_intake_and_failed_checklist(tmp_path, monkeypatch, checklist_error):
    class ChecklistFailureAI:
        name = "synthetic test engine"

        def extract(self, text, previous_error=None):
            return json.dumps(RECORD)

        def draft_checklist(self, profile):
            if checklist_error:
                raise RuntimeError("SYNTHETIC_CHECKLIST_RESPONSE")
            return " \n "

        def usage_summary(self):
            return None

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "email.txt").write_text("Synthetic intake fixture.", encoding="utf-8")
    settings = cli.load_settings(headless=True, inputs_dir=inputs)
    monkeypatch.setattr(
        cli, "load_settings", lambda **_: replace(settings, paths=replace(settings.paths, outputs=tmp_path / "outputs")),
    )
    monkeypatch.setattr(cli, "_make_ai", lambda _: ChecklistFailureAI())
    monkeypatch.setattr(cli, "setup_logging", lambda folder: folder / "run.log")

    assert cli.main(["--headless", "--inputs", str(inputs)]) == 1

    [report_path] = (tmp_path / "outputs" / "runs").glob("run-*/run_report.md")
    report = report_path.read_text(encoding="utf-8")
    assert "| Verification PASS | 1 |" in report
    assert "| Verification FAIL | 0 |" in report
    assert "| Document checklist failures after verified intake | 1 |" in report
    assert "SYNTHETIC_CHECKLIST_RESPONSE" not in report
    assert not (report_path.parent / "checklists").exists()
