"""Actual intake identity, typed values and non-destructive historical migration."""

from __future__ import annotations

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from review_fixtures import legacy_scenarios
from src.sigma_demo.pdf import source
from src.intake_contract import decode_value, field_definitions, normalize_value
from src.review_store import ReviewError, ReviewStore


def field(detail, identifier):
    return next(item for item in detail["fields"] if item["id"] == identifier)


def test_new_workspace_stays_empty_on_reopen(tmp_path):
    assert ReviewStore(tmp_path).list_returns() == []
    assert ReviewStore(tmp_path).list_returns() == []


@pytest.mark.parametrize("kind", ["individual", "business"])
def test_blank_intake_has_only_assigned_identity_and_no_source_claims(tmp_path, kind):
    store = ReviewStore(tmp_path)
    detail = store.create_return(2025, client_name="Alex Example", return_type=kind)
    assert {item["id"] for item in detail["fields"]} == {item["id"] for item in field_definitions()}
    assert detail["destination"] == "Client intake"
    assert detail["legacyFields"] == []
    for item in detail["fields"]:
        assert item["candidates"] == item["citations"] == item["dependencies"] == []
        assert item["derivation"] is None
        if item["id"] in {"full_name", "tax_year"}:
            assert item["valueOrigin"] == "workspace"
        else:
            assert item["value"] is None
            assert item["valueOrigin"] is None
    assert field(detail, "full_name")["value"] == "Alex Example"
    assert field(detail, "tax_year")["value"] == "2025"
    assert ReviewStore(tmp_path).get_return(detail["id"]) == detail


def test_same_name_clients_are_distinct_and_new_year_keeps_saved_identity(tmp_path):
    store = ReviewStore(tmp_path)
    current = store.create_return(2025, client_name="Same Name", return_type="business")
    other = store.create_return(2025, client_name="Same Name", return_type="business")
    prior = store.create_return(2024, client_id=current["clientId"])
    assert current["clientId"] != other["clientId"]
    assert prior["clientId"] == current["clientId"]
    assert prior["clientName"] == current["clientName"]
    assert field(prior, "tax_year")["value"] == "2024"
    assert store.get_return(current["id"]) == current


@pytest.mark.parametrize("year", [1989, 2200, 2025.0, True])
def test_workspace_year_matches_native_bounds(tmp_path, year):
    with pytest.raises(ReviewError):
        ReviewStore(tmp_path).create_return(year, client_name="Client", return_type="individual")


def test_concurrent_same_client_year_creation_has_one_winner(tmp_path):
    store = ReviewStore(tmp_path)
    current = store.create_return(2025, client_name="Client", return_type="individual")

    def create(_):
        try:
            return store.create_return(2024, client_id=current["clientId"])
        except ReviewError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 409 in results
    assert store.get_return(current["id"]) == current


@pytest.mark.parametrize("scenario_index", [0, 1, 2])
def test_legacy_worksheets_archive_every_fact_and_keep_documents_history(tmp_path, scenario_index):
    store = ReviewStore(tmp_path)
    scenario = legacy_scenarios()[scenario_index]
    original = copy.deepcopy(scenario["detail"])
    original["events"].append(
        {
            "id": "saved-history",
            "kind": "manual_correction",
            "fieldId": original["fields"][0]["id"],
            "message": "Earlier preparer decision",
        }
    )
    phone_id = "contact-phone" if original["returnType"] == "business" else "phone"
    phone = field(original, phone_id)
    phone.update(
        value="503-555-0101", valueOrigin="preparer", correctionReason="Verified by preparer"
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "INSERT INTO review_returns VALUES (?, ?, ?)",
            (original["id"], json.dumps(original), "historical-timestamp"),
        )
        for identifier, pdf in scenario["pdfs"].items():
            connection.execute(
                "INSERT INTO review_documents VALUES (?, ?, ?)", (original["id"], identifier, pdf)
            )
    migrated = store.get_return(original["id"])
    assert migrated["legacyFields"] == original["fields"]
    for key in (
        "id",
        "clientId",
        "clientName",
        "taxYear",
        "returnType",
        "events",
        "revision",
    ):
        assert migrated[key] == original[key]
    assert field(migrated, "phone")["value"] == "503-555-0101"
    assert field(migrated, "phone")["valueOrigin"] == "preparer"
    assert field(migrated, "street_address")["value"] is None
    assert field(migrated, "city")["value"] is None
    assert field(migrated, "number_of_dependents")["value"] is None
    assert not any(item["kind"] == "money" for item in migrated["fields"])
    assert store.list_returns()[0]["destination"] == "Client intake"
    assert store.list_returns()[0]["updatedAt"] == "historical-timestamp"
    saved = store.correct(migrated["id"], migrated["revision"], "notes", "New native note", [])
    reopened = ReviewStore(tmp_path).get_return(saved["id"])
    assert reopened == saved
    assert reopened["legacyFields"] == original["fields"]
    for identifier, pdf in scenario["pdfs"].items():
        assert store.get_pdf(saved["id"], identifier) == pdf


@pytest.mark.parametrize(
    "identifier,value,expected",
    [
        ("number_of_dependents", "0", "0"),
        ("tax_year", "2025", "2025"),
        ("filing_status", "Married filing jointly", "married_filing_jointly"),
        ("state", "Oregon", "OR"),
        ("urgency", "High", "high"),
        ("date_of_birth", "1985-02-28", "1985-02-28"),
        ("income_sources", ["W-2 wages", "rental"], '["w2_wages","rental"]'),
        ("deductions", [], "[]"),
        ("phone", "", None),
    ],
)
def test_values_normalize_for_native_control_consumers(identifier, value, expected):
    definition = next(item for item in field_definitions() if item["id"] == identifier)
    assert normalize_value(definition, value) == expected
    decoded = decode_value(definition, expected)
    assert decoded == (json.loads(expected) if definition["kind"] == "multi" else expected or "")


@pytest.mark.parametrize(
    "identifier,value",
    [
        ("number_of_dependents", "1.5"),
        ("number_of_dependents", "21"),
        ("tax_year", str(date.today().year + 1)),
        ("filing_status", "probably married"),
        ("income_sources", '["unknown"]'),
        ("income_sources", "w2_wages"),
        ("date_of_birth", "2025-02-30"),
        ("date_of_birth", "2999-01-01"),
        ("drivers_license_issued_on", "1899-01-01"),
        ("email", "not-email"),
        ("zip_code", "9720"),
    ],
)
def test_invalid_native_values_fail_without_mutation(tmp_path, identifier, value):
    store = ReviewStore(tmp_path)
    detail = store.create_return(2025, client_name="Client", return_type="individual")
    with pytest.raises(ReviewError):
        store.correct(detail["id"], detail["revision"], identifier, value, [])
    assert store.get_return(detail["id"]) == detail


def test_long_native_notes_and_explicit_empty_checkboxes_survive_restart(tmp_path):
    store = ReviewStore(tmp_path)
    detail = store.create_return(2025, client_name="Client", return_type="individual")
    note = "A preparer observation. " * 100
    detail = store.correct(detail["id"], detail["revision"], "notes", note, [])
    detail = store.correct(detail["id"], detail["revision"], "income_sources", "[]", [])
    assert field(detail, "notes")["value"] == note.strip()
    assert field(detail, "income_sources")["value"] == "[]"
    assert ReviewStore(tmp_path).get_return(detail["id"]) == detail
