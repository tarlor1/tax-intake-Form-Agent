"""Queue ownership, grounding and legacy preservation, without fake worker success."""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import sqlite3

import pytest

from src.review_jobs import ReviewJobs
from src.review_store import ReviewError, ReviewStore
from src.review_target import fillable_values, form_payload
from src.review_worker import allowed_worker_url, local_origin

TARGET = "http://127.0.0.1:8766/index.html"


@pytest.fixture
def store(tmp_path):
    return ReviewStore(tmp_path / "workspace")


@pytest.fixture
def detail(store):
    return store.create_return(2025, client_name="Avery Morgan", return_type="individual")


@pytest.fixture
def jobs(store):
    queue = ReviewJobs(store, target_url=TARGET)
    try:
        yield queue
    finally:
        queue.close()


def assert_error(code, action):
    with pytest.raises(ReviewError) as error:
        action()
    assert error.value.status_code == code


def test_one_active_browser_per_return_and_independent_clients_years(store, detail, jobs):
    first = jobs.enqueue(detail["id"], detail["revision"])
    assert first["status"] == "queued"
    assert first["target"] == "intake-form"
    assert not first["browserAvailable"] and not first["submitted"]
    assert first["intakeId"] is None and first["observedAt"] is None
    assert_error(409, lambda: jobs.enqueue(detail["id"], detail["revision"]))
    assert_error(409, lambda: jobs.ensure_idle(detail["id"]))
    prior = store.create_return(2024, client_id=detail["clientId"])
    business = store.create_return(2025, client_name="Example Studio", return_type="business")
    assert jobs.enqueue(prior["id"], prior["revision"])["returnId"] == prior["id"]
    assert jobs.enqueue(business["id"], business["revision"])["returnId"] == business["id"]
    with jobs.lock:
        assert jobs.list_jobs(detail["id"])[0]["id"] == first["id"]


def test_exact_revision_and_return_boundaries(store, detail, jobs):
    assert_error(422, lambda: jobs.enqueue(detail["id"], True))
    assert_error(409, lambda: jobs.enqueue(detail["id"], detail["revision"] + 1))
    job = jobs.enqueue(detail["id"], detail["revision"])
    other = store.create_return(2025, client_name="Another Person", return_type="individual")
    assert_error(404, lambda: jobs.cancel(other["id"], job["id"]))
    assert_error(404, lambda: jobs.browser_frame(other["id"], job["id"]))
    assert_error(404, lambda: jobs.interact(other["id"], job["id"], {"kind": "key", "key": "Tab"}))
    assert_error(404, lambda: jobs.choose_source(other["id"], job["id"], 1, "email", "unknown"))
    assert jobs.browser_frame(detail["id"], job["id"]) is None
    assert_error(409, lambda: jobs.interact(detail["id"], job["id"], {"kind": "text", "text": "x"}))


def test_cancelled_queue_never_starts_or_claims_target_submission(store, detail, jobs):
    job = jobs.enqueue(detail["id"], detail["revision"])
    cancelled = jobs.cancel(detail["id"], job["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["completedFields"] == 0
    assert not cancelled["submitted"] and not cancelled["browserAvailable"]
    assert jobs.browser_state(detail["id"])["values"] == {}
    assert jobs.cancel(detail["id"], job["id"])["status"] == "cancelled"
    assert store.get_return(detail["id"]) == detail
    jobs.ensure_idle(detail["id"])
    assert jobs.enqueue(detail["id"], detail["revision"])["id"] != job["id"]


def test_empty_and_partial_sources_can_open_an_editable_real_form(store, detail, jobs):
    for item in detail["fields"]:
        item.update(value=None, citations=[], valueOrigin=None)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE review_returns SET payload=? WHERE id=?", (json.dumps(detail), detail["id"])
        )
    values, issues = fillable_values(detail)
    assert values == {}
    assert issues
    job = jobs.enqueue(detail["id"], detail["revision"])
    assert job["totalFields"] == 0
    assert not job["submitted"]


def test_workspace_identity_is_the_only_uncited_automatic_identity(detail):
    values, _ = fillable_values(detail)
    assert values == {"full_name": "Avery Morgan", "tax_year": "2025"}
    changed = copy.deepcopy(detail)
    for item in changed["fields"]:
        if item["id"] == "full_name":
            item["value"] = "Different Person"
        if item["id"] == "phone":
            item.update(value="555-0100", valueOrigin="workspace")
    values, _ = fillable_values(changed)
    assert "full_name" not in values and "phone" not in values


def test_typed_preparer_values_map_without_citations_or_invented_defaults(store, detail):
    for field_id, value in {
        "phone": "555-0100",
        "state": "OR",
        "date_of_birth": "1985-02-12",
        "number_of_dependents": "0",
        "income_sources": '["w2_wages","rental"]',
        "deductions": "[]",
        "notes": "Human note",
    }.items():
        detail = store.correct(detail["id"], detail["revision"], field_id, value, [])
    values, _ = fillable_values(detail)
    assert values["number_of_dependents"] == "0"
    assert values["income_sources"] == ["w2_wages", "rental"]
    assert values["deductions"] == []
    assert values["state"] == "OR" and values["date_of_birth"] == "1985-02-12"
    payload = form_payload(detail)
    assert payload["urgency"] == "" and payload["drivers_license_number"] == ""
    assert payload["email"] == "" and payload["notes"] == "Human note"


@pytest.mark.parametrize("value", [None, "", " ", "\x00", "x" * 10001])
def test_unsupported_values_are_not_automatically_filled(detail, value):
    phone = next(item for item in detail["fields"] if item["id"] == "phone")
    phone.update(value=value, valueOrigin="preparer")
    assert "phone" not in fillable_values(detail)[0]


def test_conflicting_or_uncited_sources_do_not_fill_unrelated_controls(detail):
    phone = next(item for item in detail["fields"] if item["id"] == "phone")
    detail["documents"] = [{"id": "source", "pageCount": 1}]
    phone.update(
        value="555-0100",
        valueOrigin="source",
        citations=[
            {
                "documentId": "source",
                "page": 1,
                "quote": "Phone: 555-0100",
            }
        ],
    )
    assert fillable_values(detail)[0]["phone"] == "555-0100"
    phone["conflict"] = True
    assert "phone" not in fillable_values(detail)[0]
    phone["conflict"] = False
    phone["citations"][0]["page"] = 2
    assert "phone" not in fillable_values(detail)[0]
    detail["fields"].append({"id": "primary-wages", "value": "72000", "valueOrigin": "preparer"})
    assert "primary-wages" not in form_payload(detail)


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "evaluate", "script": "alert(1)"},
        {"kind": "goto", "url": TARGET},
        {"kind": "click", "x": True, "y": 1},
        {"kind": "click", "x": -1, "y": 2},
        {"kind": "click", "x": float("nan"), "y": 2},
        {"kind": "wheel", "x": 1, "y": 1, "deltaY": 2001},
        {"kind": "key", "key": "Control+L"},
        {"kind": "key", "key": "F12"},
        {"kind": "text", "text": "x" * 10001},
        {"kind": "text", "text": "\x00"},
        {"kind": "focus", "fieldId": "submit-btn"},
        {"kind": "text", "text": "fine", "script": "extra"},
    ],
)
def test_fixed_actions_reject_executable_navigation_or_unbounded_input(jobs, action):
    assert_error(422, lambda: jobs._action(action))


@pytest.mark.parametrize(
    "key", ["Tab", "Shift+Tab", "Space", "Enter", "Control+A", "Meta+A", "Shift+ArrowLeft"]
)
def test_fixed_actions_allow_native_editing_and_navigation(jobs, key):
    assert jobs._action({"kind": "key", "key": key})["key"] == key


@pytest.mark.parametrize(
    "url,method",
    [
        ("https://example.com/index.html", "GET"),
        ("http://127.0.0.1:8765/index.html", "GET"),
        ("http://127.0.0.1:8766/api/review/returns/one", "GET"),
        ("http://127.0.0.1:8766/api/intake", "GET"),
        (TARGET, "POST"),
        (TARGET + "?token=secret", "GET"),
        ("http://user:secret@127.0.0.1:8766/index.html", "GET"),
        ("file:///tmp/index.html", "GET"),
        ("javascript:alert(1)", "GET"),
        ("http://127.0.0.1:8766/another.html", "GET"),
    ],
)
def test_target_origin_and_native_method_allowlist(url, method):
    assert not allowed_worker_url(url, TARGET, method)


def test_only_original_page_and_native_submit_are_approved():
    assert local_origin(TARGET) == ("http", "127.0.0.1", 8766)
    assert allowed_worker_url(TARGET, TARGET)
    assert allowed_worker_url("http://127.0.0.1:8766/api/intake", TARGET, "POST")
    with pytest.raises(ValueError):
        local_origin("https://external.example")


def _leave_unfinished_workspace(data_dir):
    store = ReviewStore(data_dir)
    queue = ReviewJobs(store, target_url=TARGET)
    for name in ("Queued Person", "Running Person", "Closing Person"):
        detail = store.create_return(2025, client_name=name, return_type="individual")
        job = queue.enqueue(detail["id"], detail["revision"])
        status = {
            "Queued Person": "queued",
            "Running Person": "running",
            "Closing Person": "cancelling",
        }[name]
        with sqlite3.connect(store.database_path) as connection:
            connection.execute(
                "UPDATE review_job_queue SET status=? WHERE id=?", (status, job["id"])
            )
    os._exit(0)


def test_restart_interrupts_prior_ownership_without_replay(tmp_path):
    data_dir = tmp_path / "crashed-workspace"
    process = multiprocessing.get_context("spawn").Process(
        target=_leave_unfinished_workspace, args=(data_dir,)
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 0
    store = ReviewStore(data_dir)
    queue = ReviewJobs(store, target_url=TARGET)
    try:
        for summary in store.list_returns():
            job = queue.list_jobs(summary["id"])[0]
            assert job["status"] == "interrupted"
            assert not job["browserAvailable"] and not job["submitted"]
            assert queue.browser_state(summary["id"])["values"] == {}
    finally:
        queue.close()


def test_second_supervisor_cannot_steal_live_workspace(store, detail, jobs):
    assert_error(409, lambda: ReviewJobs(store, target_url=TARGET))
    assert jobs.enqueue(detail["id"], detail["revision"])["status"] == "queued"


def test_legacy_generated_history_and_saved_records_are_preserved_not_relabelled(store, detail):
    with sqlite3.connect(store.database_path) as connection:
        connection.executescript("""
            CREATE TABLE review_job_queue (
                id TEXT PRIMARY KEY,return_id TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,
                completed_fields INTEGER NOT NULL,total_fields INTEGER NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,error TEXT,issues TEXT NOT NULL,source_revision INTEGER NOT NULL,
                snapshot TEXT NOT NULL,source_signature TEXT NOT NULL,owner_id TEXT NOT NULL,
                worker_pid INTEGER,worker_identity TEXT,saved_pages TEXT NOT NULL
            );
            CREATE TABLE review_practice_values (
                return_id TEXT PRIMARY KEY,values_json TEXT NOT NULL,saved_at TEXT NOT NULL,
                source_revision INTEGER NOT NULL,source_signature TEXT NOT NULL
            );
        """)
        connection.execute(
            "INSERT INTO review_job_queue VALUES (?,?, 'ready_for_review','Old generated save',1,1,'then','then',NULL,'[]',1,?,'old','old',NULL,NULL,'[0]')",
            ("old-job", detail["id"], json.dumps(detail)),
        )
        connection.execute(
            "INSERT INTO review_practice_values VALUES (?,?, 'then',1,'old')",
            (detail["id"], '{"primary-wages":"72000.00"}'),
        )
    queue = ReviewJobs(store, target_url=TARGET)
    try:
        job = queue.list_jobs(detail["id"])[0]
        assert job["target"] == "legacy-generated"
        assert not job["submitted"] and not job["browserAvailable"]
        assert queue.browser_state(detail["id"])["jobId"] is None
        with sqlite3.connect(store.database_path) as connection:
            archived = connection.execute(
                "SELECT values_json FROM review_practice_values"
            ).fetchone()[0]
        assert json.loads(archived) == {"primary-wages": "72000.00"}
        assert store.get_return(detail["id"]) == detail
    finally:
        queue.close()
