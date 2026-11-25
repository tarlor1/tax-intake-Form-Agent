"""Run the tax client intake automation end to end.

    python main.py                              # local rule-based demo; no AI API calls
    python main.py --headless                   # no visible browser
    python main.py --online-synthetic gemini    # authorize Gemini for verified bundled samples
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from src.ai_client import IntakeAI
from src.config import Settings, load_settings
from src.input_reader import list_intake_files
from src.logging_setup import setup_logging
from src.pipeline import run_pipeline
from src.reporting import format_duration

log = logging.getLogger("intake")

# Online authorization is for these exact synthetic bytes, not everything in sample_inputs/.
_SYNTHETIC_FIXTURES = {
    "01_w2_extension_priya_nair.txt": "bb0817dbfa5f462fb0030753b48b62d2d29d92229dc2befdfe8463ea84c5ac8b",
    "02_joint_return_planning_daniel_okafor.txt": "0d2f2bdd4917a9e984e0e56a642fadb55b8850dae78da27a54fdba9702709926",
    "03_freelancer_unfiled_messy.txt": "8f97a5bb00decbb0ece6bbe367f4ef7ed913d50079b3297185c86cc66f1ad05e",
    "04_rideshare_estimated_tax_luis_ortega.pdf": "5c7d6de501527be9aa27b23757819d76ab4aa279aaab1aa5e3050e767be33fa2",
    "05_widowed_retiree_harold_jensen.txt": "b067def3d8c12f04fe60310b0d02f293ee510a29e176055dc3de4d50d6611009",
    "06_irs_notice_cp2000_angela_brooks.pdf": "e0cef44e2b212d1020db82dcbe2d7e7e263bb12d0702c4cc411eebdef0d80e52",
    "07_amend_head_of_household_jasmine_reed.txt": "f92f3d98fbb1a660906adff288861371841617a08eb78be811adadbfe6f66286",
    "08_relocation_rsu_planning_elena_vasquez.pdf": "0b606d3f635fd72996996ef564c64c5c32cb2c00c1e7f15a8d787af037ce76cc",
    "09_rental_condo_sanjay_patel.txt": "3b1f2b8914b0be899c91b8ca3f4f2569716dc0d50a157ba744270d4639b36bb7",
    "10_unfiled_years_levy_notice_terrence_walker.pdf": "67c705e3c8b0c727a81b614c434a5bec13c08201f33ed36c1c8b62858db52e96",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process tax client intake emails into the local demo form.", allow_abbrev=False,
    )
    parser.add_argument("--headless", action="store_true", help="run the browser without a window")
    parser.add_argument("--slow-mo", type=int, default=300, metavar="MS", help="delay between browser actions in headed mode (default 300)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="use the local rule-based stand-in (the default)")
    mode.add_argument(
        "--online-synthetic", choices=("gemini", "claude"), metavar="PROVIDER",
        help="authorize sending the verified bundled synthetic samples to Gemini or Claude",
    )
    parser.add_argument("--inputs", type=Path, metavar="DIR", help="folder of intake emails and PDFs (default sample_inputs/)")
    parser.add_argument("--model", help="override the model for the explicitly selected online provider")
    return parser.parse_args(argv)


@contextmanager
def _run_logging(directory: Path) -> Iterator[Path]:
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    try:
        yield setup_logging(directory)
    finally:
        for handler in root.handlers[:]:
            if handler not in previous_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)


@contextmanager
def _approved_synthetic_inputs(files: list[Path]) -> Iterator[list[Path]]:
    if {path.name for path in files} != _SYNTHETIC_FIXTURES.keys():
        raise ValueError("Online mode requires exactly the bundled synthetic sample files.")
    # Process a private copy of the bytes checked here, so a later file change cannot be sent.
    with TemporaryDirectory(prefix="portfolio-synthetic-") as directory:
        approved = []
        for source in files:
            data = source.read_bytes()
            if sha256(data).hexdigest() != _SYNTHETIC_FIXTURES[source.name]:
                raise ValueError(f"Bundled synthetic sample failed verification: {source.name}")
            path = Path(directory) / source.name
            path.write_bytes(data)
            approved.append(path)
        yield approved


def _make_ai(settings: Settings) -> IntakeAI:
    if settings.online_provider is None:
        from src.offline_ai import OfflineAI

        return OfflineAI()

    assert settings.model is not None
    if settings.online_provider == "gemini":
        from google.genai import Client
        from google.genai.client import DebugConfig

        from src.ai_client import GeminiAI

        client = Client(
            api_key=os.environ["GEMINI_API_KEY"].strip(),
            vertexai=False,
            debug_config=DebugConfig(client_mode=None, replays_directory=None, replay_id=None),
            http_options={
                "base_url": "https://generativelanguage.googleapis.com",
                "client_args": {"follow_redirects": False, "trust_env": False},
            },
        )
        return GeminiAI(model=settings.model, client=client)

    from anthropic import Anthropic, DefaultHttpxClient, omit

    from src.ai_client import ClaudeAI

    client = Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"].strip(), base_url="https://api.anthropic.com",
        default_headers={"Authorization": omit},
        timeout=180.0, max_retries=3,
        http_client=DefaultHttpxClient(follow_redirects=False, trust_env=False),
    )
    return ClaudeAI(model=settings.model, effort=settings.effort, client=client)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings(
            headless=args.headless,
            slow_mo_ms=args.slow_mo,
            online_provider=args.online_synthetic,
            inputs_dir=args.inputs,
            model=args.model,
        )
        with ExitStack() as stack:
            files = list_intake_files(settings.paths.inputs)
            if settings.online_provider:
                files = stack.enter_context(_approved_synthetic_inputs(files))
            # No output or provider client is created until all inputs are approved.
            runs = settings.paths.outputs / "runs"
            runs.mkdir(parents=True, exist_ok=True)
            output_dir = Path(mkdtemp(prefix="run-", dir=runs))
            settings = replace(settings, paths=replace(settings.paths, outputs=output_dir))
            log_file = stack.enter_context(_run_logging(settings.paths.logs))
            try:
                ai = _make_ai(settings)
                if settings.online_provider:
                    log.info("Online mode authorized for %s using verified bundled synthetic data.", settings.online_provider)
                else:
                    log.info("Offline mode: local rule-based processing. No external AI requests.")
                log.info("AI engine: %s", ai.name)
                output = run_pipeline(settings, ai, files)
            except Exception as exc:
                # SDK and browser errors can contain request bodies or field values.
                log.error("Could not complete the portfolio intake demo (%s).", type(exc).__name__)
                log.info("Log:    %s", log_file)
                return 2

            s = output.summary
            log.info(
                "Done in %s: %d processed, %d submission attempts (%d PASS, %d FAIL), "
                "%d checklist failure(s), %d follow-up, %d human review, %d error",
                format_duration(s.duration_s), s.processed, s.submitted, s.passed, s.failed,
                s.checklist_failures, s.follow_ups, s.needs_review, s.errors,
            )
            log.info("Report: %s", output.pdf_path or output.report_path)
            if output.pdf_path:
                log.info("        %s (Markdown copy)", output.report_path)
            log.info("Log:    %s", log_file)
            return 1 if s.needs_attention else 0
    except (ValueError, OSError) as exc:
        print(f"Could not start the portfolio intake demo: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
