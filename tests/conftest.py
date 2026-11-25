import os
from pathlib import Path
from uuid import uuid4

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A release run must not pass by skipping a requested integration scenario."""
    if os.environ.get("SIGMA_RELEASE_TESTS") != "1" or exitstatus != pytest.ExitCode.OK:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and reporter.stats.get("skipped"):
        reporter.write_sep("=", "Release checks skipped required scenarios", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def coordinator_db():
    """Use only the explicitly supplied Unix socket of a disposable test server."""
    socket_dir = os.environ.get("SIGMA_TEST_POSTGRES_SOCKET")
    if not socket_dir:
        pytest.skip("Set SIGMA_TEST_POSTGRES_SOCKET to a disposable PostgreSQL server socket directory")
    socket_path = Path(socket_dir)
    if not socket_path.is_absolute() or not socket_path.is_dir():
        pytest.fail("SIGMA_TEST_POSTGRES_SOCKET must be an existing absolute socket directory")

    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    from src.preparation.coordinator_db import CoordinatorDB

    schema = f"sigma_test_{uuid4().hex}"

    def connect(**options):
        return psycopg.connect(
            host=str(socket_path), port=int(os.environ.get("SIGMA_TEST_POSTGRES_PORT", "5432")),
            dbname="postgres", row_factory=dict_row, connect_timeout=2, **options,
        )

    with connect(autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        db = CoordinatorDB(lambda: connect(options=f"-c search_path={schema},pg_catalog"))
        try:
            db.install_core()
            yield db
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
