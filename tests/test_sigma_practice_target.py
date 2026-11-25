"""Exercise the independent practice draft solely through its native browser UI."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from shutil import which

import pytest
from playwright.sync_api import Error, expect, sync_playwright

from src.sigma_demo.target import sigma_practice_target


CLIENT = "TEST-PERSON-01"
W2S = {
    "TEST-EMP-A": ("A-1", "30000", "1600"),
    "TEST-EMP-B": ("B-1", "30000", "800"),
}
RECEIPTS = ("ROTOR-102", "ROTOR-841")
EXPENSE = {"Description": "North Auto: Supplies", "Amount": "45.00"}


def _launch_chromium(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except Error as exc:
        if "Executable doesn't exist" not in str(exc):
            raise
        # A cache may have the full browser but not the headless shell, or CI
        # may provide Chromium as a system package instead of a Playwright cache.
        cached_browser = playwright.chromium.executable_path
        executable = (
            cached_browser if Path(cached_browser).is_file()
            else which("chromium") or which("chromium-browser")
        )
        if not executable:
            pytest.skip("Install Chromium with python -m playwright install chromium")
        return playwright.chromium.launch(headless=True, executable_path=executable)


@contextmanager
def _isolated_browser(playwright, target_url):
    browser = _launch_chromium(playwright)
    external_urls = []

    def restrict_to_target(route):
        if route.request.url.startswith(target_url):
            route.continue_()
        else:
            external_urls.append(route.request.url)
            route.abort()

    try:
        context = browser.new_context(service_workers="block")
        context.route("**/*", restrict_to_target)
        yield context.new_page()
    finally:
        browser.close()
    assert not external_urls, f"Practice draft attempted external requests: {external_urls}"


def _fill(page, values):
    for label, value in values.items():
        page.get_by_label(label, exact=True).fill(value)


def _search(page, reference=CLIENT, year="2025"):
    _fill(page, {"Client reference": reference, "Tax year": year})
    page.get_by_label("Return type").select_option("individual")
    page.get_by_role("button", name="Search", exact=True).click()


def _w2_fields(reference):
    instance, wages, withholding = W2S[reference]
    return {
        "Employer reference": reference, "Form instance": instance,
        "Box 1 wages": wages, "Box 2 federal withholding": withholding,
    }


def _record(page, table_name, reference):
    table = page.get_by_role("table", name=table_name)
    return table.get_by_role("row").filter(has=page.get_by_role("cell", name=reference, exact=True))


def _record_ids(page, table_name, references):
    table = page.get_by_role("table", name=table_name)
    expect(table.locator("tbody tr > td:first-child")).to_have_text(list(references))
    ids = {reference: _record(page, table_name, reference).get_attribute("data-record-id")
           for reference in references}
    assert all(ids.values()) and len(set(ids.values())) == len(references)
    return ids


def test_draft_reopens_with_saved_rows_and_without_uncommitted_values(tmp_path):
    database = tmp_path / "practice.sqlite3"
    with sync_playwright() as playwright:
        with sigma_practice_target(database) as url, _isolated_browser(playwright, url) as page:
            page.goto(url)
            _search(page)
            expect(page.get_by_role("status")).to_have_text("No matching draft.")
            page.get_by_role("button", name="Create draft").click()
            draft = page.get_by_role("main")
            draft_id = draft.get_attribute("data-draft-id")
            assert draft_id
            expect(draft).to_have_attribute("data-status", "unfiled")
            expect(draft).to_have_attribute("data-client-reference", CLIENT)
            expect(draft).to_have_attribute("data-tax-year", "2025")
            expect(draft).to_have_attribute("data-return-type", "individual")

            # Insert in reverse display order. The same wages cannot distinguish these records.
            for reference in reversed(W2S):
                page.get_by_role("link", name="Add W-2").click()
                _fill(page, _w2_fields(reference))
                page.get_by_role("button", name="Save W-2").click()
            w2_ids = _record_ids(page, "W-2 records", W2S)
            _record(page, "W-2 records", "TEST-EMP-B").get_by_role("link", name="Edit W-2").click()
            _fill(page, {"Box 1 wages": "99999"})
            page.get_by_role("link", name="Back to W-2s").click()

            page.get_by_role("link", name="Rideshare", exact=True).click()
            _fill(page, {"Gross receipts": "6200", "Platform fees": "600"})
            page.get_by_role("button", name="Save rideshare").click()
            _fill(page, {"Gross receipts": "5600"})  # Opening a child does not save the parent.
            page.get_by_role("link", name="Expenses", exact=True).click()
            expect(page.get_by_role("dialog", name="Expenses", exact=True)).to_be_visible()
            for reference in reversed(RECEIPTS):
                page.get_by_role("link", name="Add expense").click()
                expect(page.get_by_role("dialog", name="New expense", exact=True)).to_be_visible()
                _fill(page, {"Receipt reference": reference, **EXPENSE})
                page.get_by_role("button", name="Save expense").click()
            expense_ids = _record_ids(page, "Expense records", RECEIPTS)

            page.get_by_role("link", name="Add expense").click()
            _fill(page, {"Receipt reference": "UNSAVED-01", **EXPENSE})
            page.get_by_role("link", name="Back to rideshare").click()
            expect(page.get_by_label("Gross receipts", exact=True)).to_have_value("6200")
            expect(page.get_by_text("Saved expense count: 2", exact=False)).to_be_visible()
            page.get_by_role("link", name="Return list").click()
            expect(page.get_by_role("heading", name="Return list")).to_be_visible()

        # Reopen from a fresh Chromium instance and a new server using the same database.
        with sigma_practice_target(database) as url, _isolated_browser(playwright, url) as page:
            page.goto(url)
            for reference, year in ((CLIENT.lower(), "2025"), (CLIENT, "2024")):
                _search(page, reference, year)
                expect(page.get_by_role("status")).to_have_text("No matching draft.")
                expect(page.get_by_role("link", name="Open draft")).to_have_count(0)
            _search(page)
            result = page.get_by_role("region", name="Exact search result")
            expect(result).to_have_attribute("data-draft-id", draft_id)
            expect(page.get_by_role("button", name="Create draft")).to_have_count(0)
            result.get_by_role("link", name="Open draft").click()
            expect(page.get_by_role("main")).to_have_attribute("data-draft-id", draft_id)
            assert _record_ids(page, "W-2 records", W2S) == w2_ids
            for reference in W2S:
                _record(page, "W-2 records", reference).get_by_role("link", name="Edit W-2").click()
                expect(page.locator("form[data-record-id]")).to_have_attribute("data-record-id", w2_ids[reference])
                for label, value in _w2_fields(reference).items():
                    expect(page.get_by_label(label, exact=True)).to_have_value(value)
                page.get_by_role("link", name="Back to W-2s").click()

            page.get_by_role("link", name="Rideshare", exact=True).click()
            expect(page.get_by_label("Gross receipts", exact=True)).to_have_value("6200")
            expect(page.get_by_label("Platform fees", exact=True)).to_have_value("600")
            page.get_by_role("link", name="Expenses", exact=True).click()
            assert _record_ids(page, "Expense records", RECEIPTS) == expense_ids
            for reference in RECEIPTS:
                _record(page, "Expense records", reference).get_by_role("link", name="Edit expense").click()
                expect(page.locator("form[data-record-id]")).to_have_attribute("data-record-id", expense_ids[reference])
                for label, value in {"Receipt reference": reference, **EXPENSE}.items():
                    expect(page.get_by_label(label, exact=True)).to_have_value(value)
                page.get_by_role("link", name="Back to expenses").click()
