"""Launch the local Sigma evidence companion."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import webbrowser
from contextlib import ExitStack
from pathlib import Path
from subprocess import SubprocessError

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review Sigma draft evidence in a separate local window."
    )
    parser.add_argument("--port", type=int, default=8765, help="loopback HTTP port (default 8765)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "outputs" / "review_workspace",
        help="persistent workspace directory; existing reviews are never reset",
    )
    parser.add_argument("--no-browser", action="store_true", help="serve without opening a browser")
    parser.add_argument(
        "--practice-gemini", action="store_true",
        help="enable Gemini draft preparation in the synthetic browser target for approved public samples",
    )
    parser.add_argument(
        "--inspect-archive", action="store_true",
        help="open the legacy SQLite archive read-only instead of starting the inbox",
    )
    parser.add_argument(
        "--sigma-config", type=Path,
        help="firm-managed Sigma enrollment file; without it, use the local inbox",
    )
    parser.add_argument(
        "--native-review-factory", metavar="MODULE:FACTORY",
        help="trusted firm-installed factory for the authorized local Sigma review connector",
    )

    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.native_review_factory and not args.sigma_config:
        parser.error("--native-review-factory requires --sigma-config")
    if args.inspect_archive and args.sigma_config:
        parser.error("--inspect-archive cannot be combined with --sigma-config")
    if args.practice_gemini and (args.inspect_archive or args.sigma_config):
        parser.error("--practice-gemini is available only in the local workspace")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import uvicorn
        from psycopg import Error as PostgresError
        from src.local_postgres import local_postgres
        from src.preparation.local_practice import local_practice
        from src.preparation.practice_inbox_adapter import PracticeInboxAdapter
        from src.preparation.sigma_config import SigmaConfig
        from src.preparation.site import load_site_factory
        from src.review_api import create_app
        from src.review_store import ReviewError
    except ImportError as exc:
        print(
            f"Review dependencies are missing ({exc.name}). Run: pip install -r review-requirements.txt",
            file=sys.stderr,
        )
        return 2

    with ExitStack() as resources:
        try:
            config = SigmaConfig.read(args.sigma_config) if args.sigma_config else None
            native_factory = load_site_factory(args.native_review_factory) if args.native_review_factory else None
            local_db = None if config or args.inspect_archive else resources.enter_context(
                local_postgres(args.data_dir.expanduser().absolute() / "inbox-postgres")
            )
            preparation = None
            if args.practice_gemini and local_db is not None:
                worker = resources.enter_context(local_practice(local_db, args.data_dir, args.port))
                preparation = PracticeInboxAdapter(local_db, worker)
            app = create_app(
                args.data_dir, port=args.port, sigma_config=config,
                native_review_factory=native_factory, local_db=local_db, preparation=preparation,
            )
        except (ReviewError, RuntimeError, ValueError, OSError, sqlite3.Error, PostgresError, SubprocessError) as exc:
            print(f"Review setup error: {exc}", file=sys.stderr)
            return 2

        return _serve(args, uvicorn, app, enrolled=config is not None)


def _serve(args: argparse.Namespace, uvicorn, app, *, enrolled: bool) -> int:

    url = f"http://127.0.0.1:{args.port}"
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=args.port, log_level="info",
            proxy_headers=False, server_header=False, access_log=False,
        )
    )

    if not args.no_browser:
        def open_when_ready() -> None:
            pause = threading.Event()
            for _ in range(300):
                if server.started:
                    webbrowser.open(url)
                    return
                if server.should_exit:
                    return
                pause.wait(0.1)

        threading.Thread(target=open_when_ready, daemon=True).start()

    mode = ("firm service" if enrolled else "read-only legacy archive" if args.inspect_archive
            else "persistent local inbox — Gemini practice enabled" if args.practice_gemini
            else "persistent local inbox — local document extraction")
    print(
        f"Review workspace: {url}\n"
        f"Mode: {mode}",
        flush=True,
    )
    try:
        server.run()
    except KeyboardInterrupt:
        return 130
    return 0 if server.started else 2


if __name__ == "__main__":
    raise SystemExit(main())
