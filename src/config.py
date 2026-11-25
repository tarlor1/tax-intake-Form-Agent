"""Runtime settings, loaded from .env and command-line flags."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_INPUTS = PROJECT_ROOT / "sample_inputs"
OnlineProvider = Literal["gemini", "claude"]

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Paths:
    inputs: Path
    form_dir: Path
    outputs: Path

    @property
    def followups(self) -> Path:
        return self.outputs / "followups"

    @property
    def screenshots(self) -> Path:
        return self.outputs / "screenshots"

    @property
    def checklists(self) -> Path:
        return self.outputs / "checklists"

    @property
    def needs_review(self) -> Path:
        return self.outputs / "needs_review"

    @property
    def logs(self) -> Path:
        return self.outputs / "logs"

    @property
    def report(self) -> Path:
        return self.outputs / "run_report.md"

    @property
    def report_pdf(self) -> Path:
        return self.outputs / "run_report.pdf"


@dataclass(frozen=True)
class Settings:
    model: str | None
    effort: str
    headless: bool
    slow_mo_ms: int
    online_provider: OnlineProvider | None
    paths: Paths
    custom_inputs: bool = False


def load_settings(
    *,
    headless: bool = False,
    slow_mo_ms: int = 300,
    online_provider: OnlineProvider | None = None,
    inputs_dir: Path | None = None,
    model: str | None = None,
) -> Settings:
    """Resolve local defaults; read online configuration only with explicit authorization."""
    if online_provider not in (None, "gemini", "claude"):
        raise ValueError("Online provider must be gemini or claude.")
    if online_provider and inputs_dir is not None:
        raise ValueError("--online-synthetic is limited to the bundled samples and cannot be used with --inputs.")
    if model is not None and online_provider is None:
        raise ValueError("--model requires --online-synthetic.")

    effort = DEFAULT_EFFORT
    chosen_model = None
    if online_provider is not None:
        load_dotenv(PROJECT_ROOT / ".env")
        if online_provider == "gemini":
            key_name, model_name, default_model = "GEMINI_API_KEY", "GEMINI_MODEL", DEFAULT_GEMINI_MODEL
        else:
            key_name, model_name, default_model = "ANTHROPIC_API_KEY", "CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL
            if os.getenv("ANTHROPIC_CUSTOM_HEADERS"):
                raise ValueError("Unset ANTHROPIC_CUSTOM_HEADERS for the bundled synthetic demo.")
            effort = os.getenv("CLAUDE_EFFORT", DEFAULT_EFFORT).strip().lower()
            if effort not in EFFORT_LEVELS:
                raise ValueError(f"CLAUDE_EFFORT must be one of {', '.join(EFFORT_LEVELS)}.")
        if not os.getenv(key_name, "").strip():
            raise ValueError(f"{key_name} is required for the selected online provider.")
        chosen_model = model.strip() if model is not None else os.getenv(model_name, "").strip() or default_model
        if not chosen_model:
            raise ValueError("--model must not be empty.")

    return Settings(
        model=chosen_model,
        effort=effort,
        headless=headless,
        slow_mo_ms=0 if headless else max(slow_mo_ms, 0),
        online_provider=online_provider,
        paths=Paths(
            inputs=(inputs_dir if inputs_dir is not None else BUNDLED_INPUTS).resolve(),
            form_dir=PROJECT_ROOT / "intake_form",
            outputs=PROJECT_ROOT / "outputs",
        ),
        custom_inputs=inputs_dir is not None,
    )
