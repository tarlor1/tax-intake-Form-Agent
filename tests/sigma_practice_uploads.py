"""Committed uploads and ordered outbox delivery for synthetic practice tests only."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


class PracticeObjects:
    """Private files standing in for immutable object storage in tests."""

    def __init__(self, directory: Path):
        self.directory = directory

    def put_if_absent(self, firm_id: str, digest: str, content: bytes) -> str:
        target = self.directory / firm_id / digest
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            temporary = target.with_name(uuid4().hex)
            try:
                temporary.write_bytes(content)
                try:
                    target.hardlink_to(temporary)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        assert target.read_bytes() == content
        return digest

    def read(self, firm_id: str, object_id: str) -> bytes | None:
        try:
            return (self.directory / firm_id / object_id).read_bytes()
        except FileNotFoundError:
            return None

    def checksum(self, firm_id: str, object_id: str) -> str | None:
        content = self.read(firm_id, object_id)
        return sha256(content).hexdigest() if content is not None else None


@dataclass(frozen=True)
class UploadSlot:
    slot_id: str
    return_id: str | None = None
    input_revision: int | None = None
    grant_id: str | None = None


@dataclass(frozen=True)
class UploadCommitted:
    position: int
    version_id: str


@dataclass(frozen=True)
class UploadCandidate:
    version_id: str
    firm_id: str
    batch_id: str
    slot_id: str
    return_id: str | None
    input_revision: int | None
    authorized: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    firm_id TEXT NOT NULL, batch_id TEXT NOT NULL,
    PRIMARY KEY (firm_id, batch_id)
);
CREATE TABLE IF NOT EXISTS slots (
    firm_id TEXT NOT NULL, batch_id TEXT NOT NULL, slot_id TEXT NOT NULL,
    return_id TEXT, input_revision INTEGER, grant_id TEXT,
    PRIMARY KEY (firm_id, batch_id, slot_id),
    FOREIGN KEY (firm_id, batch_id) REFERENCES batches (firm_id, batch_id)
);
CREATE TABLE IF NOT EXISTS grants (
    firm_id TEXT NOT NULL, batch_id TEXT NOT NULL, grant_id TEXT NOT NULL,
    PRIMARY KEY (firm_id, batch_id, grant_id),
    FOREIGN KEY (firm_id, batch_id) REFERENCES batches (firm_id, batch_id)
);
CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY, contents BLOB NOT NULL,
    firm_id TEXT NOT NULL, batch_id TEXT NOT NULL, slot_id TEXT NOT NULL,
    UNIQUE (firm_id, batch_id, slot_id),
    FOREIGN KEY (firm_id, batch_id, slot_id) REFERENCES slots (firm_id, batch_id, slot_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    position INTEGER PRIMARY KEY, version_id TEXT NOT NULL UNIQUE REFERENCES versions
);
"""


class UploadWriter:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def declare_batch(
        self,
        firm_id: str,
        batch_id: str,
        slots: tuple[UploadSlot, ...],
        *,
        approved_grants: tuple[str, ...] = (),
    ) -> None:
        """Freeze the manifest and its synthetic, batch-scoped grant references."""
        if not slots or len({slot.slot_id for slot in slots}) != len(slots):
            raise ValueError("A manifest needs nonempty, distinct slots")
        if any(
            (slot.return_id is None) != (slot.input_revision is None)
            or (slot.input_revision is not None and slot.input_revision < 1)
            for slot in slots
        ):
            raise ValueError("An assigned slot needs both a return and a positive revision")
        scope = (firm_id, batch_id)
        approved = set(approved_grants)
        if not self._db.execute(
            "INSERT INTO batches (firm_id, batch_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            scope,
        ).rowcount:
            declared = self._db.execute(
                "SELECT slot_id, return_id, input_revision, grant_id FROM slots "
                "WHERE firm_id = ? AND batch_id = ?",
                scope,
            )
            grants = self._db.execute(
                "SELECT grant_id FROM grants WHERE firm_id = ? AND batch_id = ?", scope
            )
            if {UploadSlot(*row) for row in declared} != set(slots) or {
                row[0] for row in grants
            } != approved:
                raise ValueError("Cannot change a declared manifest or its grants")
            return
        self._db.executemany(
            "INSERT INTO slots VALUES (?, ?, ?, ?, ?, ?)",
            ((*scope, s.slot_id, s.return_id, s.input_revision, s.grant_id) for s in slots),
        )
        self._db.executemany("INSERT INTO grants VALUES (?, ?, ?)", ((*scope, g) for g in approved))

    def put_file(self, firm_id: str, batch_id: str, slot_id: str, contents: bytes) -> str:
        """Return the immutable version ID; identical retransmissions publish nothing."""
        scope = (firm_id, batch_id, slot_id)
        existing = self._db.execute(
            "SELECT version_id, contents FROM versions WHERE firm_id = ? AND batch_id = ? AND slot_id = ?",
            scope,
        ).fetchone()
        if existing is not None:
            version_id, stored = existing
            if contents != stored:
                raise ValueError("Cannot replace the bytes of an uploaded slot")
            return version_id
        if self._db.execute(
            "SELECT 1 FROM slots WHERE firm_id = ? AND batch_id = ? AND slot_id = ?", scope
        ).fetchone() is None:
            raise ValueError("Upload slot was not declared")
        version_id = uuid4().hex
        self._db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?)", (version_id, contents, *scope))
        self._db.execute("INSERT INTO outbox (version_id) VALUES (?)", (version_id,))
        return version_id


class PracticeUploads:
    """Isolated upstream database with one durable event per committed version."""

    def __init__(self, path: Path):
        self._path = path
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL").fetchone()
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            yield db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @contextmanager
    def writer(self) -> Iterator[UploadWriter]:
        """Commit on normal exit; roll back both uploads and events on exception."""
        with self._transaction() as db:
            yield UploadWriter(db)

    def feed(self, after: int = 0) -> tuple[UploadCommitted, ...]:
        """Read committed outbox events. An event alone does not imply batch completion."""
        with self._connect() as db:
            return self._feed(db, after)

    @staticmethod
    def _feed(
        db: sqlite3.Connection, after: int, *, limit: int = -1
    ) -> tuple[UploadCommitted, ...]:
        return tuple(
            UploadCommitted(*row)
            for row in db.execute(
                "SELECT position, version_id FROM outbox WHERE position > ? ORDER BY position LIMIT ?",
                (after, limit),
            )
        )

    def read_version(self, firm_id: str, version_id: str) -> bytes | None:
        with self._connect() as db:
            return self._read_version(db, firm_id, version_id)

    @staticmethod
    def _read_version(db: sqlite3.Connection, firm_id: str, version_id: str) -> bytes | None:
        row = db.execute(
            "SELECT contents FROM versions WHERE firm_id = ? AND version_id = ?",
            (firm_id, version_id),
        ).fetchone()
        return None if row is None else row[0]

    def candidate(self, event: UploadCommitted) -> UploadCandidate | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT v.version_id, v.firm_id, v.batch_id, s.slot_id, s.return_id, s.input_revision,
                          g.grant_id IS NOT NULL
                   FROM outbox e JOIN versions v USING (version_id)
                   JOIN slots s USING (firm_id, batch_id, slot_id)
                   LEFT JOIN grants g ON g.firm_id = s.firm_id AND g.batch_id = s.batch_id
                                     AND g.grant_id = s.grant_id
                   WHERE e.position = ? AND e.version_id = ?""",
                (event.position, event.version_id),
            ).fetchone()
        return UploadCandidate(*row[:-1], bool(row[-1])) if row else None
