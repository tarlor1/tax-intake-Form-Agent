"""Keep one writer in a qualified interactive session, independently of leases."""

from collections.abc import Generator
from contextlib import contextmanager
import errno
import os
from pathlib import Path


class SessionBusy(RuntimeError):
    pass


@contextmanager
def hold_session(lock_path: Path) -> Generator[None, None, None]:
    """Hold a lock in the firm-provisioned protected location for this native seat.

    Worker and review launcher must share the same local installation lock path.
    Never delete the file: Unix locks are inode-bound. Process exit drops this
    lock but does not release the central grant or prove the vendor UI quiescent.
    """
    if not lock_path.is_absolute() or not lock_path.parent.is_dir():
        raise ValueError("The session lock requires a provisioned absolute local path.")

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            try:
                # Windows permits locking beyond EOF, so no marker bytes are needed.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EPERM, errno.EACCES, errno.EDEADLK}:
                    raise SessionBusy("The interactive tax software session is already reserved.") from None
                raise
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionBusy("The interactive tax software session is already reserved.") from None
        yield
    finally:
        os.close(fd)
