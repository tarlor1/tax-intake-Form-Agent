"""Launch the companion and the unchanged, independently hosted intake website."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import webbrowser
from contextlib import ExitStack
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Document intake and UI-only Playwright control of intake_form/index.html."
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
        "--workers",
        type=int,
        default=2,
        help="maximum concurrent local browser workers (default 2)",
    )
    parser.add_argument(
        "--show-workers",
        action="store_true",
        help="show worker browser windows instead of running headless",
    )
    parser.add_argument(
        "--intake-url",
        help="existing loopback intake website URL; default starts intake_form/index.html separately",
    )

    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 1 <= args.workers <= 32:
        parser.error("--workers must be between 1 and 32 (capacity must be measured)")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import uvicorn
        from src.form_server import IntakeServer
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
            target_url = args.intake_url
            if target_url is None:
                target = resources.enter_context(IntakeServer(PROJECT_ROOT / "intake_form"))
                target_url = target.url + "index.html"

            app = create_app(
                args.data_dir,
                port=args.port,
                max_workers=args.workers,
                headless=not args.show_workers,
                target_url=target_url,
            )
        except (ReviewError, RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
            print(f"Review setup error: {exc}", file=sys.stderr)
            return 2

        url = f"http://127.0.0.1:{args.port}"
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="info",
                proxy_headers=False,
                server_header=False,
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

        print(
            f"Review workspace: {url}\n"
            f"Intake website: {target_url}\n"
            f"Persistent data: {args.data_dir.expanduser().resolve()}\nWorker limit: {args.workers}",
            flush=True,
        )
        server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
