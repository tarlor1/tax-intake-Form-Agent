"""Human decisions, evidence isolation, and controller HTTP boundaries."""

from __future__ import annotations

import pytest

from review_fixtures import BUSINESS_ID, INDIVIDUAL_ID, PRIOR_YEAR_ID, seeded_store
from src.review_store import ReviewError, ReviewStore


def field(detail, identifier):
    return next(item for item in detail["fields"] if item["id"] == identifier)


@pytest.fixture
def store(tmp_path):
    return seeded_store(tmp_path / "workspace")


def test_changing_field_focus_preserves_unresolved_values_and_evidence(store):
    original = store.get_return(INDIVIDUAL_ID)
    selected = store.select(original["id"], original["revision"], "street_address")
    changed = store.select(selected["id"], selected["revision"], "phone")

    assert changed["activeFieldId"] == "phone"
    assert field(changed, "street_address") == field(original, "street_address")
    assert field(changed, "phone") == field(original, "phone")


def test_source_resolution_requires_review_and_preserves_other_facts(store):
    detail = store.get_return(INDIVIDUAL_ID)
    candidate = field(detail, "street_address")["candidates"][-1]
    selected = store.resolve(detail["id"], detail["revision"], "street_address", candidate["id"])
    assert field(selected, "street_address")["status"] == "needs_review"
    assert field(selected, "street_address")["value"] == "28 Sample Avenue"
    assert field(selected, "city") == field(detail, "city")

    focused = store.select(selected["id"], selected["revision"], "email")
    assert field(focused, "street_address") == field(selected, "street_address")


def test_stale_revision_rejects_without_partial_writes(store):
    original = store.get_return(INDIVIDUAL_ID)
    changed = store.select(original["id"], original["revision"], "email")
    with pytest.raises(ReviewError):
        store.correct(original["id"], original["revision"], "phone", "503-555-0101", [])
    assert store.get_return(original["id"]) == changed


def test_candidates_and_citations_belong_to_exact_field_client_and_year(store):
    detail = store.get_return(INDIVIDUAL_ID)
    for other_id, identifier in [
        (PRIOR_YEAR_ID, "email"),
        (BUSINESS_ID, "email"),
        (INDIVIDUAL_ID, "employer"),
    ]:
        other = field(store.get_return(other_id), identifier)
        with pytest.raises(ReviewError):
            store.resolve(detail["id"], detail["revision"], "email", other["candidates"][0]["id"])
        with pytest.raises(ReviewError):
            store.correct(
                detail["id"],
                detail["revision"],
                "email",
                "new@example.test",
                [other["citations"][0]["id"]],
            )
    assert store.get_return(detail["id"]) == detail


def test_restart_retains_review_events_originals_and_legacy_facts(store):
    original = store.get_return(INDIVIDUAL_ID)
    updated = store.correct(
        original["id"],
        original["revision"],
        "number_of_dependents",
        "0",
        [],
        "Verified no dependents",
    )
    updated = store.select(updated["id"], updated["revision"], "phone")
    assert ReviewStore(store.data_dir).get_return(updated["id"]) == updated
    assert updated["legacyFields"] == original["legacyFields"]
    for document in updated["documents"]:
        assert ReviewStore(store.data_dir).get_pdf(updated["id"], document["id"]) == store.get_pdf(
            updated["id"], document["id"]
        )


