"""Durable ownership and observations for independent, retained intake browsers."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import multiprocessing
import os
import signal
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from .intake_contract import decode_value, field_definitions, normalize_value
from .review_store import ReviewError, ReviewStore, _candidate_origin
from .review_target import fillable_values, form_payload
from .review_worker import (
    ALLOWED_KEYS,
    BROWSER_VIEWPORT,
    MAX_BROWSER_FRAME_BYTES,
    local_origin,
    run_worker,
)

MAX_RETAINED_BROWSER_FRAMES = 64
MAX_PENDING_ACTIONS = 32


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _snapshot(detail: dict) -> dict:
    return copy.deepcopy(
        {
            key: detail[key]
            for key in (
                "id",
                "clientId",
                "clientName",
                "taxYear",
                "revision",
                "fields",
                "documents",
            )
        }
    )


def _signature(detail: dict) -> str:
    # Finding a local highlight must not invalidate an already-owned browser snapshot.
    fields = [
        {
            key: (
                [
                    citation
                    for citation in item.get("citations", [])
                    if not citation.get("localMatch")
                ]
                if key == "citations"
                else item.get(key)
            )
            for key in (
                "id",
                "value",
                "citations",
                "dependencies",
                "derivation",
                "conflict",
                "valueOrigin",
            )
        }
        for item in detail["fields"]
    ]
    data = {
        "id": detail["id"],
        "clientId": detail["clientId"],
        "taxYear": detail["taxYear"],
        "fields": fields,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _process_identity(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        start_ticks = stat[stat.rfind(")") + 2 :].split()[19]
        return f"{Path('/proc/sys/kernel/random/boot_id').read_text().strip()}:{start_ticks}"
    except (OSError, IndexError):
        return None


def _signal_group(pid: int, identity: str | None, signum: int, job_id: str) -> None:
    if identity is not None and _process_identity(pid) == identity:
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signum)
            else:
                os.kill(pid, signum)
        except ProcessLookupError:
            pass
        return
    # A dead leader may leave browser descendants. The inherited marker prevents
    # killing an unrelated, subsequently reused process group.
    markers = {f"REVIEW_BROWSER_JOB={job_id}".encode(), f"REVIEW_PRACTICE_JOB={job_id}".encode()}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rfind(")") + 2 :].split()
            if int(fields[2]) != pid or fields[0] == "Z":
                continue
            if not markers.intersection((entry / "environ").read_bytes().split(b"\0")):
                continue
            os.killpg(pid, signum)
            return
        except (OSError, ValueError, IndexError):
            continue


@dataclass
class _Running:
    process: BaseProcess
    messages: Connection
    cancellation: ProcessEvent
    pid: int
    identity: str | None
    deadline: float
    retained: bool = False
    stop_at: float | None = None
    kill_at: float | None = None
    result: dict | None = None
    forced_error: str | None = None
    commands: deque = field(default_factory=deque)
    inflight: dict | None = None
    committed_values: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)


class ReviewJobs:
    """One OS-leased supervisor owns every browser and all SQLite mutations.

    Automation slots are released at handoff. At most max(4,max_workers) browser
    processes are retained; capacity queues new work, never evicts unsaved work.
    Retained browsers have no silent expiry. Cancel/close is explicit, and a new
    browser cannot start for that return until the old process has been reaped.
    """

    def __init__(
        self, store: ReviewStore, *, target_url: str, max_workers: int = 2, headless: bool = True
    ):
        local_origin(target_url)
        if urlsplit(target_url).path != "/index.html":
            raise ValueError("The independent intake target URL must end in /index.html.")
        if type(max_workers) is not int or not 1 <= max_workers <= 32:
            raise ValueError("max_workers must be between 1 and 32.")

        self.store = store
        self.target_url = target_url
        self.max_workers = max_workers
        self.max_retained_sessions = max(4, max_workers)
        self.headless = headless
        self.lock = threading.RLock()
        self._owner = uuid.uuid4().hex

        self._context = multiprocessing.get_context("spawn")
        self._running: dict[str, _Running] = {}
        self._frames: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._closing = self._closed = False
        self._definitions = {item["id"]: item for item in field_definitions()}

        self._lease = Path(store.database_path).with_suffix(".queue.lock").open("a+b")
        try:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lease.close()
            raise ReviewError(
                409, "Another local process owns this workspace's browser queue."
            ) from None

        try:
            self._initialize()
        except BaseException:
            self._lease.close()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.store.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS review_job_queue (
                    id TEXT PRIMARY KEY, return_id TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, completed_fields INTEGER NOT NULL,
                    total_fields INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error TEXT, issues TEXT NOT NULL,
                    source_revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    source_signature TEXT NOT NULL, owner_id TEXT NOT NULL,
                    worker_pid INTEGER, worker_identity TEXT, saved_pages TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS review_job_one_active_return
                    ON review_job_queue(return_id)
                    WHERE status IN ('queued', 'running', 'cancelling');
                CREATE TABLE IF NOT EXISTS review_browser_receipts (
                    job_id TEXT NOT NULL, intake_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, values_json TEXT NOT NULL,
                    PRIMARY KEY(job_id,intake_id)
                );
            """)

            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(review_job_queue)")
            }
            additions = {
                "target": "TEXT NOT NULL DEFAULT 'legacy-generated'",
                "browser_available": "INTEGER NOT NULL DEFAULT 0",
                "focused_field_id": "TEXT",
                "submitted": "INTEGER NOT NULL DEFAULT 0",
                "intake_id": "TEXT",
                "observed_at": "TEXT",
                "values_json": "TEXT NOT NULL DEFAULT '{}'",
                "action_pending": "INTEGER NOT NULL DEFAULT 0",
                "action_error": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE review_job_queue ADD COLUMN {name} {declaration}"
                    )

            # Obsolete review_practice_values is intentionally untouched/read-only.
            previous = connection.execute(
                "SELECT * FROM review_job_queue WHERE worker_pid IS NOT NULL"
            ).fetchall()
            for row in previous:
                _signal_group(row["worker_pid"], row["worker_identity"], signal.SIGKILL, row["id"])
            connection.execute(
                """
                UPDATE review_job_queue SET status='interrupted',stage=?,error=?,updated_at=?
                WHERE status IN ('queued','running','cancelling') OR browser_available=1
            """,
                (
                    "Browser unavailable after previous process exit; explicitly open again",
                    "Previous browser ownership ended; no automatic replay was attempted.",
                    _now(),
                ),
            )
            connection.execute("""
                UPDATE review_job_queue SET browser_available=0,action_pending=0,
                    worker_pid=NULL,worker_identity=NULL
            """)

    def _open(self) -> None:
        if self._closed or self._closing:
            raise ReviewError(503, "The local browser queue is shutting down.")

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "returnId": row["return_id"],
            "status": row["status"],
            "stage": row["stage"],
            "completedFields": row["completed_fields"],
            "totalFields": row["total_fields"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "error": row["error"],
            "issues": json.loads(row["issues"]),
            "sourceRevision": row["source_revision"],
            "target": row["target"],
            "browserAvailable": bool(row["browser_available"]),
            "focusedFieldId": row["focused_field_id"],
            "submitted": bool(row["submitted"]),
            "intakeId": row["intake_id"],
            "observedAt": row["observed_at"],
            "actionPending": bool(row["action_pending"]),
            "actionError": row["action_error"],
        }

    @staticmethod
    def _job(connection: sqlite3.Connection, return_id: str, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM review_job_queue WHERE id=? AND return_id=?", (job_id, return_id)
        ).fetchone()
        if row is None:
            raise ReviewError(404, "Browser job not found in this return.")
        return row

    def start(self) -> None:
        with self.lock:
            self._open()
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._supervise, name="local-intake-queue", daemon=True
                )
                self._thread.start()
                self._wake.set()

    def list_jobs(self, return_id: str) -> list[dict]:
        with self.lock:
            self.store.get_return(return_id)
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM review_job_queue WHERE return_id=? ORDER BY created_at DESC,rowid DESC",
                    (return_id,),
                ).fetchall()
            return [self._public(row) for row in rows]

    def browser_state(self, return_id: str) -> dict:
        with self.lock:
            self.store.get_return(return_id)
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM review_job_queue WHERE return_id=? AND target='intake-form' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (return_id,),
                ).fetchone()
            if row is None:
                return {
                    "jobId": None,
                    "values": {},
                    "observedAt": None,
                    "submitted": False,
                    "intakeId": None,
                    "browserAvailable": False,
                    "focusedFieldId": None,
                    "issues": [],
                }
            return {
                "jobId": row["id"],
                "values": json.loads(row["values_json"]),
                "observedAt": row["observed_at"],
                "submitted": bool(row["submitted"]),
                "intakeId": row["intake_id"],
                "browserAvailable": bool(row["browser_available"]),
                "focusedFieldId": row["focused_field_id"],
                "issues": json.loads(row["issues"]),
            }

    def browser_frame(self, return_id: str, job_id: str) -> bytes | None:
        with self.lock:
            self.store.get_return(return_id)
            with self._connection() as connection:
                row = self._job(connection, return_id, job_id)
            if row["owner_id"] != self._owner:
                return None
            frame_value = self._frames.get(job_id)
            return frame_value[1] if frame_value and frame_value[0] == return_id else None

    def _remember_frame(self, return_id: str, job_id: str, jpeg: bytes) -> None:
        self._frames[job_id] = (return_id, jpeg)
        self._frames.move_to_end(job_id)
        while len(self._frames) > MAX_RETAINED_BROWSER_FRAMES:
            oldest = next((key for key in self._frames if key not in self._running), None)
            if oldest is None:
                break
            del self._frames[oldest]

    def ensure_idle(self, return_id: str) -> None:
        with self.lock:
            self._open()
            # Existence belongs to the caller's store transaction; a newly created
            # return may not yet be visible through a separate connection.
            with self._connection() as connection:
                active = connection.execute(
                    "SELECT 1 FROM review_job_queue WHERE return_id=? AND (status IN ('queued','running','cancelling') OR browser_available=1)",
                    (return_id,),
                ).fetchone()
            if active is not None:
                raise ReviewError(
                    409,
                    "This return owns a browser. Use the actual form or explicitly close it before changing source data or reopening.",
                )

    def enqueue(self, return_id: str, expected_revision: int) -> dict:
        with self.lock:
            self._open()
            self.ensure_idle(return_id)
            detail = self.store.get_return(return_id)
            if type(expected_revision) is not int or expected_revision < 1:
                raise ReviewError(422, "expectedRevision must be a positive integer.")
            if detail["revision"] != expected_revision:
                raise ReviewError(409, "This return changed. Reload before opening the browser.")

            snapshot = _snapshot(detail)
            values, issues = fillable_values(snapshot)
            job_id, now = uuid.uuid4().hex, _now()
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO review_job_queue
                    (id,return_id,status,stage,completed_fields,total_fields,created_at,updated_at,
                     error,issues,source_revision,snapshot,source_signature,owner_id,saved_pages,target)
                    VALUES (?,?,'queued',?,0,?,?,?,NULL,?,?,?,?,?,'[]','intake-form')
                """,
                    (
                        job_id,
                        return_id,
                        "Queued for independent intake browser",
                        len(values),
                        now,
                        now,
                        json.dumps(issues),
                        expected_revision,
                        json.dumps(snapshot),
                        _signature(snapshot),
                        self._owner,
                    ),
                )
                result = self._public(self._job(connection, return_id, job_id))

            for old_id in [
                key for key, frame_value in self._frames.items() if frame_value[0] == return_id
            ]:
                del self._frames[old_id]
            self._wake.set()
            return result

    def cancel(self, return_id: str, job_id: str) -> dict:
        with self.lock:
            self._open()
            self.store.get_return(return_id)
            with self._connection() as connection:
                row = self._job(connection, return_id, job_id)
                if row["status"] == "queued":
                    connection.execute(
                        "UPDATE review_job_queue SET status='cancelled',stage=?,updated_at=? WHERE id=?",
                        ("Cancelled before browser launch", _now(), job_id),
                    )
                elif row["status"] == "running" or row["browser_available"]:
                    connection.execute(
                        "UPDATE review_job_queue SET status='cancelling',browser_available=0,action_pending=0,stage=?,updated_at=? WHERE id=?",
                        ("Closing owned browser; waiting for process cleanup", _now(), job_id),
                    )
                    running = self._running.get(job_id)
                    if running is not None:
                        running.commands.clear()
                        running.cancellation.set()
                        running.stop_at = time.monotonic() + 1.5
                result = self._public(self._job(connection, return_id, job_id))
            self._wake.set()
            return result

    def _interactive(self, return_id: str, job_id: str) -> tuple[dict, _Running]:
        self._open()
        self.store.get_return(return_id)
        with self._connection() as connection:
            row = self._job(connection, return_id, job_id)

        running = self._running.get(job_id)
        if (
            row["owner_id"] != self._owner
            or not row["browser_available"]
            or row["status"] != "ready_for_review"
            or running is None
            or not running.retained
        ):
            raise ReviewError(409, "The actual browser is not available for human interaction.")
        return dict(row), running

    @staticmethod
    def _reason(reason: object) -> str:
        if not isinstance(reason, str) or len(reason) > 2000 or "\x00" in reason:
            raise ReviewError(422, "The optional provenance note must be at most 2000 characters.")
        return reason.strip()

    def _action(self, action: dict) -> dict:
        if not isinstance(action, dict):
            raise ReviewError(422, "A fixed browser action is required.")

        kind = action.get("kind")
        keys = {
            "click": {"x", "y"},
            "wheel": {"x", "y", "deltaY"},
            "key": {"key"},
            "text": {"text"},
            "focus": {"fieldId"},
        }
        if (
            not isinstance(kind, str)
            or kind not in keys
            or set(action) - ({"kind", "reason"} | keys[kind])
            or not keys[kind] <= set(action)
        ):
            raise ReviewError(422, "Unsupported browser action or arguments.")

        result = {key: action[key] for key in {"kind"} | keys[kind]}
        result["reason"] = self._reason(action.get("reason", ""))

        if kind in {"click", "wheel"}:
            for coordinate, bound in (
                ("x", BROWSER_VIEWPORT["width"]),
                ("y", BROWSER_VIEWPORT["height"]),
            ):
                value = action[coordinate]
                if (
                    type(value) not in {int, float}
                    or not math.isfinite(value)
                    or not 0 <= value < bound
                ):
                    raise ReviewError(422, "Coordinates must be native image viewport pixels.")
            if kind == "wheel" and (
                type(action["deltaY"]) not in {int, float}
                or not math.isfinite(action["deltaY"])
                or abs(action["deltaY"]) > 2000
            ):
                raise ReviewError(422, "Wheel delta must be finite and at most 2000 pixels.")
        elif kind == "key" and (
            not isinstance(action["key"], str) or action["key"] not in ALLOWED_KEYS
        ):
            raise ReviewError(422, "This key is not approved for form editing.")
        elif kind == "text" and (
            not isinstance(action["text"], str)
            or len(action["text"]) > 10000
            or "\x00" in action["text"]
        ):
            raise ReviewError(422, "Browser text must contain at most 10000 characters and no NUL.")
        elif kind == "focus" and (
            not isinstance(action["fieldId"], str) or action["fieldId"] not in self._definitions
        ):
            raise ReviewError(422, "Focus must name an actual intake control.")
        return result

    def _queue_action(
        self,
        return_id: str,
        job_id: str,
        running: _Running,
        action: dict,
        source: dict | None = None,
        edit: dict | None = None,
    ) -> dict:
        if len(running.commands) + (running.inflight is not None) >= MAX_PENDING_ACTIONS:
            raise ReviewError(
                429, "Browser action queue is full. Wait for acknowledgement before retrying."
            )

        running.commands.append(
            {"id": uuid.uuid4().hex, "action": action, "source": source, "edit": edit}
        )
        with self._connection() as connection:
            connection.execute(
                "UPDATE review_job_queue SET action_pending=1,action_error=NULL,updated_at=? WHERE id=?",
                (_now(), job_id),
            )
            result = self._public(self._job(connection, return_id, job_id))
        self._wake.set()
        return result

    def interact(self, return_id: str, job_id: str, action: dict) -> dict:
        with self.lock:
            _, running = self._interactive(return_id, job_id)
            return self._queue_action(return_id, job_id, running, self._action(action))

    def apply_field(
        self,
        return_id: str,
        job_id: str,
        expected_revision: int,
        field_id: str,
        value: str,
        *,
        reason: str = "",
    ) -> dict:
        with self.lock:
            _, running = self._interactive(return_id, job_id)
            if running.commands or running.inflight:
                raise ReviewError(409, "Wait for the current form change before applying another value.")

            detail = self.store.get_return(return_id)
            if type(expected_revision) is not int or expected_revision != detail["revision"]:
                raise ReviewError(409, "This return changed. Reload before applying this value.")
            definition = self._definitions.get(field_id) if isinstance(field_id, str) else None
            if definition is None:
                raise ReviewError(422, "Choose an actual field in this return.")
            if not isinstance(value, str) or len(value) > 10000 or "\x00" in value:
                raise ReviewError(422, "A field value must contain at most 10000 characters and no NUL.")

            try:
                normalized = normalize_value(definition, value)
                native_value = decode_value(definition, normalized)
            except (ValueError, TypeError) as error:
                raise ReviewError(422, str(error)) from error

            action = {
                "kind": "field",
                "fieldId": field_id,
                "value": native_value,
                "reason": self._reason(reason),
            }
            edit = {
                "fieldId": field_id,
                "expectedRevision": expected_revision,
                "value": normalized or "",
            }
            return self._queue_action(return_id, job_id, running, action, edit=edit)

    def submit_browser(
        self, return_id: str, job_id: str, expected_revision: int
    ) -> dict:
        with self.lock:
            row, running = self._interactive(return_id, job_id)
            if running.commands or running.inflight:
                raise ReviewError(409, "Wait for pending form changes before submitting.")

            detail = self.store.get_return(return_id)
            if type(expected_revision) is not int or expected_revision != detail["revision"]:
                raise ReviewError(409, "This return changed. Reload and review before submitting.")
            if row["submitted"]:
                raise ReviewError(409, "The actual form already has a submission receipt.")
            if any(item.get("conflict") for item in detail["fields"]):
                raise ReviewError(409, "Resolve conflicting field values before submitting.")

            return self._queue_action(return_id, job_id, running, {"kind": "submit"})

    def choose_source(
        self,
        return_id: str,
        job_id: str,
        expected_revision: int,
        field_id: str,
        candidate_id: str,
        *,
        reason: str = "",
    ) -> dict:
        with self.lock:
            _, running = self._interactive(return_id, job_id)
            if running.commands or running.inflight:
                raise ReviewError(409, "Wait for pending browser actions before choosing a source.")

            detail = self.store.get_return(return_id)
            if type(expected_revision) is not int or expected_revision != detail["revision"]:
                raise ReviewError(409, "This return changed. Reload before choosing a source.")
            definition = self._definitions.get(field_id) if isinstance(field_id, str) else None
            item = next((item for item in detail["fields"] if item["id"] == field_id), None)
            candidate = next(
                (item for item in (item or {}).get("candidates", []) if item["id"] == candidate_id),
                None,
            )
            if definition is None or candidate is None:
                raise ReviewError(422, "Candidate must belong to an actual field in this return.")

            proposed = copy.deepcopy(detail)
            selected = next(item for item in proposed["fields"] if item["id"] == field_id)
            selected.update(
                value=candidate["value"],
                citations=candidate["citations"],
                conflict=False,
                selectedCandidateId=candidate_id,
                valueOrigin=_candidate_origin(candidate),
            )
            supported, _ = fillable_values(proposed)
            if field_id not in supported:
                raise ReviewError(
                    422, "This source lacks usable evidence or a valid actual-form value."
                )

            reason = self._reason(reason)
            action = {
                "kind": "source",
                "fieldId": field_id,
                "value": supported[field_id],
                "reason": reason,
            }
            source = {
                "fieldId": field_id,
                "candidateId": candidate_id,
                "expectedRevision": expected_revision,
            }
            return self._queue_action(return_id, job_id, running, action, source)

    def _validate_snapshot(self, row: sqlite3.Row, detail: dict) -> dict:
        snapshot = json.loads(row["snapshot"])
        if (
            row["owner_id"] != self._owner
            or row["return_id"] != detail["id"]
            or snapshot["id"] != detail["id"]
            or snapshot["clientId"] != detail["clientId"]
            or snapshot["taxYear"] != detail["taxYear"]
            or snapshot["revision"] != row["source_revision"]
            or row["source_revision"] != detail["revision"]
            or row["source_signature"] != _signature(detail)
            or row["source_signature"] != _signature(snapshot)
        ):
            raise ReviewError(
                409, "The owning browser's source snapshot no longer matches this return."
            )
        return snapshot

    def _launch(self, row: sqlite3.Row) -> None:
        try:
            snapshot = self._validate_snapshot(row, self.store.get_return(row["return_id"]))
        except ReviewError as exc:
            self._terminal(row["id"], "failed", exc.detail)
            return

        parent, child = self._context.Pipe(duplex=True)
        cancellation = self._context.Event()
        process = self._context.Process(
            target=run_worker,
            args=(
                snapshot,
                self.target_url,
                row["id"],
                child,
                cancellation,
                self.headless,
                os.getpid(),
            ),
            name=f"intake-{row['id'][:8]}",
        )
        with self._connection() as connection:
            connection.execute(
                "UPDATE review_job_queue SET status='running',stage=?,updated_at=? WHERE id=?",
                ("Starting independent intake browser", _now(), row["id"]),
            )

        try:
            process.start()
        except BaseException as exc:
            parent.close()
            child.close()
            self._terminal(
                row["id"], "failed", f"Could not start the browser worker: {type(exc).__name__}."
            )
            return

        child.close()
        pid = process.pid
        assert pid is not None
        identity = _process_identity(pid)
        self._running[row["id"]] = _Running(
            process, parent, cancellation, pid, identity, time.monotonic() + 120
        )
        with self._connection() as connection:
            connection.execute(
                "UPDATE review_job_queue SET worker_pid=?,worker_identity=? WHERE id=?",
                (pid, identity, row["id"]),
            )

    def _terminal(self, job_id: str, status: str, error: str | None = None) -> None:
        stages = {
            "cancelled": "Closed; owned browser and worker stopped",
            "failed": "Actual browser unavailable",
        }
        with self._connection() as connection:
            connection.execute(
                "UPDATE review_job_queue SET status=?,stage=?,error=?,browser_available=0,action_pending=0,worker_pid=NULL,worker_identity=NULL,updated_at=? WHERE id=? AND owner_id=?",
                (status, stages[status], error, _now(), job_id, self._owner),
            )

    def _record_observation(
        self,
        row: sqlite3.Row,
        running: _Running,
        state: dict,
        *,
        initial: bool = False,
        reason: str = "",
        skip: set[str] | None = None,
    ) -> None:
        values = state.get("values")
        if not isinstance(values, dict) or set(values) != set(self._definitions):
            raise ReviewError(409, "Actual form controls no longer match the intake contract.")
        for field_id, value in values.items():
            if (
                not isinstance(value, (str, list))
                or (isinstance(value, str) and (len(value) > 10000 or "\x00" in value))
                or (
                    isinstance(value, list)
                    and (
                        len(value) > 32
                        or any(not isinstance(v, str) or len(v) > 100 for v in value)
                    )
                )
            ):
                raise ReviewError(
                    409, "Actual form observation exceeds the supported field bounds."
                )

        detail = self.store.get_return(row["return_id"])
        if initial:
            running.committed_values = copy.deepcopy(values)
        else:
            commits = set(state.get("commitFields", []))
            for field_id, value in values.items():
                if value == running.committed_values.get(field_id) or field_id in (skip or set()):
                    continue
                if reason:
                    running.reasons[field_id] = reason
                if field_id not in commits:
                    continue
                definition = self._definitions[field_id]
                try:
                    normalized = normalize_value(definition, value)
                except (ValueError, TypeError):
                    continue  # Partial/invalid native edits remain editable and observed.
                item = next(item for item in detail["fields"] if item["id"] == field_id)
                if item.get("value") != normalized:
                    detail = self.store.correct(
                        row["return_id"],
                        detail["revision"],
                        field_id,
                        normalized or "",
                        [],
                        running.reasons.get(field_id, ""),
                    )
                running.committed_values[field_id] = copy.deepcopy(value)
                running.reasons.pop(field_id, None)

        focused = state.get("focusedFieldId")
        focused = focused if focused in self._definitions else None
        submitted = (
            state.get("submitted") is True
            and isinstance(state.get("intakeId"), str)
            and 0 < len(state["intakeId"]) <= 100
        )
        issues = [item[:500] for item in state.get("issues", []) if isinstance(item, str)]
        issues.extend(
            f"{item['label']}: conflicting sources; choose a source or enter your own value."
            for item in detail["fields"]
            if item.get("conflict")
        )
        issues = list(dict.fromkeys(issues))[:64]

        with self._connection() as connection:
            connection.execute(
                """
                UPDATE review_job_queue SET values_json=?,observed_at=?,focused_field_id=?,
                    submitted=?,intake_id=?,issues=?,stage=?,updated_at=? WHERE id=?
            """,
                (
                    json.dumps(values),
                    _now(),
                    focused,
                    int(submitted),
                    state["intakeId"] if submitted else None,
                    json.dumps(issues),
                    "Actual intake confirmation observed"
                    if submitted
                    else "Filled/observed locally; not submitted",
                    _now(),
                    row["id"],
                ),
            )
            if submitted:
                connection.execute(
                    "INSERT OR IGNORE INTO review_browser_receipts VALUES (?,?,?,?)",
                    (row["id"], state["intakeId"], _now(), json.dumps(values)),
                )

    def _message(self, job_id: str, running: _Running, message: dict) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_job_queue WHERE id=? AND owner_id=?", (job_id, self._owner)
            ).fetchone()
        if row is None or row["status"] == "cancelling" or running.stop_at is not None:
            return

        kind = message.get("kind")
        if kind == "frame":
            jpeg = message.get("jpeg")
            if (
                isinstance(jpeg, bytes)
                and 4 <= len(jpeg) <= MAX_BROWSER_FRAME_BYTES
                and jpeg.startswith(b"\xff\xd8")
                and jpeg.endswith(b"\xff\xd9")
            ):
                self._remember_frame(row["return_id"], job_id, jpeg)
        elif kind == "stage":
            with self._connection() as connection:
                connection.execute(
                    "UPDATE review_job_queue SET stage=?,completed_fields=?,updated_at=? WHERE id=?",
                    (
                        str(message.get("stage", ""))[:250],
                        min(
                            row["total_fields"],
                            max(0, message.get("completedFields", row["completed_fields"])),
                        ),
                        _now(),
                        job_id,
                    ),
                )
        elif kind == "handoff":
            snapshot = self._validate_snapshot(row, self.store.get_return(row["return_id"]))
            if message.get("values") != form_payload(snapshot):
                raise ReviewError(
                    409, "Actual form readback does not match the grounded source snapshot."
                )
            self._record_observation(row, running, message, initial=True)
            running.retained = True
            with self._connection() as connection:
                connection.execute(
                    "UPDATE review_job_queue SET status='ready_for_review',browser_available=1,completed_fields=total_fields,updated_at=? WHERE id=?",
                    (_now(), job_id),
                )
        elif kind == "observation" and running.retained:
            command = running.inflight or {}
            decision = command.get("source") or command.get("edit")
            self._record_observation(
                row, running, message, skip={decision["fieldId"]} if decision else None
            )
        elif kind == "ack" and running.inflight and message.get("id") == running.inflight["id"]:
            command = running.inflight
            error = message.get("error")
            state = message.get("observation")
            source = command.get("source")
            edit = command.get("edit")
            decision = source or edit

            if not error and source:
                if (
                    not isinstance(state, dict)
                    or state.get("values", {}).get(source["fieldId"]) != command["action"]["value"]
                ):
                    error = "Actual source selection readback did not match. No source decision was recorded."
                else:
                    try:
                        self.store.resolve(
                            row["return_id"],
                            source["expectedRevision"],
                            source["fieldId"],
                            source["candidateId"],
                            reason=command["action"].get("reason", ""),
                        )
                    except ReviewError as exc:
                        error = exc.detail

            if not error and edit:
                if (
                    not isinstance(state, dict)
                    or state.get("values", {}).get(edit["fieldId"]) != command["action"]["value"]
                ):
                    error = "Actual field readback did not match. Your edit was not saved."
                else:
                    try:
                        self.store.correct(
                            row["return_id"],
                            edit["expectedRevision"],
                            edit["fieldId"],
                            edit["value"],
                            [],
                            command["action"].get("reason", ""),
                        )
                    except ReviewError as exc:
                        error = exc.detail

            if isinstance(state, dict):
                if decision and decision["fieldId"] in state.get("values", {}):
                    # A failed managed change must not later become an implicit human edit.
                    running.committed_values[decision["fieldId"]] = copy.deepcopy(
                        state["values"][decision["fieldId"]]
                    )
                self._record_observation(
                    row,
                    running,
                    state,
                    reason=command["action"].get("reason", ""),
                    skip={decision["fieldId"]} if decision else None,
                )

            running.inflight = None
            with self._connection() as connection:
                connection.execute(
                    "UPDATE review_job_queue SET action_pending=?,action_error=?,updated_at=? WHERE id=?",
                    (
                        int(bool(running.commands)),
                        str(error)[:1500] if error else None,
                        _now(),
                        job_id,
                    ),
                )
        elif kind in {"failed", "cancelled"}:
            running.result = message

    def _finish(self, job_id: str, running: _Running) -> None:
        _signal_group(running.pid, running.identity, signal.SIGKILL, job_id)
        running.process.join(timeout=0)
        running.messages.close()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM review_job_queue WHERE id=?", (job_id,)
            ).fetchone()
        if row["status"] == "cancelling" or self._closing:
            self._terminal(job_id, "cancelled")
        else:
            error = (
                running.forced_error
                or (running.result or {}).get("error")
                or "Actual browser exited; its last local observations are not proof of submission."
            )
            self._terminal(job_id, "failed", error[:1500])
        running.process.close()
        del self._running[job_id]

    def _tick(self) -> None:
        now = time.monotonic()
        for job_id, running in list(self._running.items()):
            try:
                while running.messages.poll():
                    message = running.messages.recv()
                    if isinstance(message, dict):
                        self._message(job_id, running, message)
                if (
                    running.retained
                    and running.inflight is None
                    and running.commands
                    and running.stop_at is None
                ):
                    command = running.commands.popleft()
                    running.inflight = command
                    running.messages.send({"id": command["id"], "action": command["action"]})
            except (EOFError, OSError):
                pass
            except ReviewError as exc:
                running.forced_error = exc.detail
                running.cancellation.set()
                running.stop_at = now

            if not running.process.is_alive():
                self._finish(job_id, running)
                continue
            if not running.retained and now >= running.deadline and running.stop_at is None:
                running.forced_error = (
                    "Browser automation exceeded its bounded execution time. Explicitly open again."
                )
                running.cancellation.set()
                running.stop_at = now
            if running.stop_at is not None and now >= running.stop_at and running.kill_at is None:
                _signal_group(running.pid, running.identity, signal.SIGTERM, job_id)
                running.kill_at = now + 1
            if running.kill_at is not None and now >= running.kill_at:
                _signal_group(running.pid, running.identity, signal.SIGKILL, job_id)

        if not self._closing:
            active = sum(not running.retained for running in self._running.values())
            capacity = min(
                self.max_workers - active, self.max_retained_sessions - len(self._running)
            )
            if capacity > 0:
                with self._connection() as connection:
                    rows = connection.execute(
                        "SELECT * FROM review_job_queue WHERE status='queued' AND owner_id=? ORDER BY created_at,rowid LIMIT ?",
                        (self._owner, capacity),
                    ).fetchall()
                for row in rows:
                    self._launch(row)

    def _supervise(self) -> None:
        try:
            while True:
                self._wake.wait(0.1)
                self._wake.clear()
                with self.lock:
                    self._tick()
                    if self._closing and not self._running:
                        return
        except BaseException:
            with self.lock:
                self._closing = True
                owned = list(self._running.items())
                for job_id, running in owned:
                    _signal_group(running.pid, running.identity, signal.SIGKILL, job_id)
            for _, running in owned:
                running.process.join(timeout=1)
                running.messages.close()
            with self.lock:
                self._running.clear()
                try:
                    with self._connection() as connection:
                        connection.execute(
                            "UPDATE review_job_queue SET status='failed',stage=?,error=?,browser_available=0,action_pending=0,updated_at=? WHERE owner_id=? AND (status IN ('queued','running','cancelling') OR browser_available=1)",
                            (
                                "Queue supervision unavailable",
                                "Owned browsers were stopped after a local supervision failure.",
                                _now(),
                                self._owner,
                            ),
                        )
                except sqlite3.Error:
                    pass

    def close(self) -> None:
        with self.lock:
            if self._closed:
                return
            self._closing = True
            for running in self._running.values():
                running.cancellation.set()
                if running.stop_at is None:
                    running.stop_at = time.monotonic() + 1.5
            self._wake.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=6)
        with self.lock:
            owned = list(self._running.items())
            for job_id, running in owned:
                _signal_group(running.pid, running.identity, signal.SIGKILL, job_id)
        for _, running in owned:
            running.process.join(timeout=1)
            running.messages.close()
            if not running.process.is_alive():
                running.process.close()
        with self.lock:
            try:
                self._running.clear()
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE review_job_queue SET status='cancelled',stage=?,browser_available=0,action_pending=0,worker_pid=NULL,worker_identity=NULL,updated_at=? WHERE owner_id=? AND (status IN ('queued','running','cancelling') OR browser_available=1)",
                        ("Closed; owned browser and worker stopped", _now(), self._owner),
                    )
            finally:
                self._frames.clear()
                self._closed = True
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_UN)
                self._lease.close()
