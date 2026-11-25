"""Private Unix-socket PostgreSQL lifecycle for the local workspace."""

from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory

import psycopg
from psycopg.rows import dict_row

from src.preparation.coordinator_db import CoordinatorDB


@contextmanager
def local_postgres(directory: Path, *, bin_dir: str | None = None) -> Generator[CoordinatorDB, None, None]:
    if os.name != "posix":
        raise RuntimeError("The local inbox requires PostgreSQL server tools and Unix sockets.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise RuntimeError("The local PostgreSQL directory must be private to the current OS user.")
    bindir = Path(bin_dir or os.environ.get("SIGMA_PG_BIN") or subprocess.check_output(
        ["pg_config", "--bindir"], text=True,
    ).strip())
    initdb, pg_ctl = bindir / "initdb", bindir / "pg_ctl"
    if not initdb.is_file() or not pg_ctl.is_file():
        raise RuntimeError("Install PostgreSQL server tools or set SIGMA_PG_BIN to their directory.")

    cluster = directory / "pg"
    flags = {"check": True, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL}
    if not (cluster / "PG_VERSION").is_file():
        subprocess.run([
            initdb, "-D", cluster, "--username=practice", "--encoding=UTF8", "--no-locale",
            "--auth-local=trust", "--auth-host=reject",
        ], **flags)
    with TemporaryDirectory(prefix="sigma-pg-") as socket:
        subprocess.run([
            pg_ctl, "-D", cluster, "-l", directory / "postgres.log", "-w", "start",
            "-o", f"-h '' -k {shlex.quote(socket)} -p 5432",
        ], start_new_session=True, **flags)
        try:
            def connect():
                return psycopg.connect(
                    host=socket, port=5432, dbname="postgres", user="practice",
                    row_factory=dict_row, connect_timeout=2,
                )

            yield CoordinatorDB(connect)
        finally:
            subprocess.run([pg_ctl, "-D", cluster, "-w", "stop", "-m", "fast"], **flags)
