"""Run the firm-local Sigma coordinator, upload relay, or enrolled worker."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
import os
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="pre-provisioned dedicated PostgreSQL schema")
    parser.add_argument("--site", metavar="MODULE:FACTORY", help="trusted firm-installed integration factory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply numbered migrations; requires no native or model access")
    serve = commands.add_parser("serve", help="serve the firm API behind a firm-controlled HTTPS reverse proxy")
    serve.add_argument("--port", type=int, default=8766)
    for command in ("dispatch", "worker"):
        loop = commands.add_parser(command)
        loop.add_argument("--once", action="store_true", help="process one bounded pass and exit")
    return parser


async def _run(pass_once: Callable[[], Awaitable], once: bool) -> None:
    while True:
        work = await pass_once()
        if once:
            return
        if not work:
            await asyncio.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "migrate" and not args.site:
        parser.error("--site is required for an enrolled runtime")
    if args.command == "serve" and not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")

    from psycopg import Error as DatabaseError
    from src.preparation.coordinator_db import connect_coordinator, migrate
    from src.preparation.site import FirmRuntime, FirmSite, load_site_factory

    try:
        db = connect_coordinator(os.environ.get("SIGMA_COORDINATOR_DSN", ""), schema=args.schema)
        if args.command == "migrate":
            version = migrate(db)
            print(f"Coordinator schema version {version}")
            return 0
        site = load_site_factory(args.site)(db)
        if not isinstance(site, FirmSite):
            raise ValueError("The coordinator factory must return a FirmSite.")
        runtime = FirmRuntime(db, site)
        if args.command == "serve":
            import uvicorn

            uvicorn.run(runtime.app(), host="127.0.0.1", port=args.port, proxy_headers=False,
                        access_log=False, server_header=False)
        elif args.command == "dispatch":
            asyncio.run(_run(runtime.dispatch_once, args.once))
        else:
            asyncio.run(_run(runtime.worker().run_once, args.once))
    except (DatabaseError, OSError):
        print("The dedicated coordinator database or protected storage is unavailable.", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError) as exc:
        print(f"Coordinator setup or execution blocked: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
