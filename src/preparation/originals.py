"""Content-addressed originals in a firm-provisioned protected directory."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 10 * 1024 * 1024


class FilesystemOriginals:
    def __init__(self, directory: Path):
        if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
            raise ValueError("Provision an absolute protected originals directory first.")
        self._directory = directory.resolve()
        if os.name == "posix" and self._directory.stat().st_mode & 0o077:
            raise ValueError("The originals directory must be private to the service account.")

    def _path(self, firm_id: str, digest: str) -> Path:
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError("Invalid immutable object identity.")
        firm_folder = hashlib.sha256(firm_id.encode()).hexdigest()
        return self._directory / firm_folder / digest

    def put_if_absent(self, firm_id: str, digest: str, content: bytes) -> str:
        if not 0 < len(content) <= _MAX_BYTES or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Bytes do not match the bounded immutable object.")
        path = self._path(firm_id, digest)
        path.parent.mkdir(exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise ValueError("An originals directory cannot be a symbolic link.")

        if not path.exists():
            with NamedTemporaryFile(dir=path.parent, prefix="incoming-", delete=False) as pending:
                temporary = Path(pending.name)
                try:
                    pending.write(content)
                    pending.flush()
                    os.fsync(pending.fileno())
                    pending.close()
                    os.link(temporary, path)
                except FileExistsError:
                    pass  # Race between commits of the same immutable content.
                finally:
                    temporary.unlink(missing_ok=True)
        if self.checksum(firm_id, digest) != digest:
            raise OSError("An existing immutable original does not match its object identity.")
        if os.name == "posix":
            # The outbox may commit only after the directory entries are durable.
            for directory in (path.parent, self._directory):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return digest

    def read(self, firm_id: str, object_id: str) -> bytes | None:
        path = self._path(firm_id, object_id)
        if path.is_symlink() or path.parent.is_symlink():
            return None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as reader:
            metadata = os.fstat(reader.fileno())
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_BYTES:
                return None
            content = reader.read(_MAX_BYTES + 1)
        return content if hashlib.sha256(content).hexdigest() == object_id else None

    def checksum(self, firm_id: str, object_id: str) -> str | None:
        return object_id if self.read(firm_id, object_id) is not None else None
