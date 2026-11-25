"""Assemble the opt-in, synthetic-only local practice workflow."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from functools import partial
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

from dotenv import dotenv_values

from ..config import DEFAULT_GEMINI_MODEL, PROJECT_ROOT
from .agent import StagePolicy
from .coordinator_db import CoordinatorDB
from .dispatch import ExtractionPolicy
from .providers import ProviderIdentity, open_provider

if TYPE_CHECKING:
    from .local_practice_worker import LocalPracticeWorker

# Published samples are independently identified by their unchanged bytes.
# File names, upload hints, and the consent checkbox cannot classify a file.
PUBLIC_SAMPLE_YEARS = {
    "a346fa66977d79e48df90571a1ad99d993251cdcc1bb744a1173388fcf8f301e": 2026,
    "1e6504ec5d09fe8aaa36d8b9469ee0109021bd5384f0a700055d30d5c8955782": 2026,
    "108e3de223f6c4123f22dea68927d56bef60d14e227b73ed9b69f2809915015d": 2026,
}


def approval_recipient(policy: StagePolicy) -> ExtractionPolicy:
    identity = policy.identity
    return ExtractionPolicy(identity.provider, identity.account, identity.model, "synthetic", identity.endpoint)


@contextmanager
def local_practice(db: CoordinatorDB, data_dir: Path, port: int) -> Iterator[LocalPracticeWorker]:
    from .local_practice_worker import LocalPracticeWorker
    from .sigma_practice_destination import sigma_practice_destination

    settings = dotenv_values(PROJECT_ROOT / ".env", interpolate=False)

    def setting(name: str) -> str:
        return (os.environ.get(name, settings.get(name)) or "").strip()

    key = setting("GEMINI_API_KEY")
    model = setting("SIGMA_PRACTICE_GEMINI_MODEL") or setting("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    if not key or any(not "!" <= ch <= "~" for ch in key):
        raise ValueError("Set GEMINI_API_KEY for --practice-gemini.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", model):
        raise ValueError("Set GEMINI_MODEL to a Gemini tool-calling model ID.")
    identity = ProviderIdentity("gemini", model, "https://generativelanguage.googleapis.com", "local-public-practice")
    policy = StagePolicy(identity, "reasoning", frozenset({"synthetic"}), 24, 65536, 180, 16384, 2048,
                         Decimal("10"), Decimal("40"), Decimal("6"))
    open_reasoning = partial(open_provider, identity, api_key=key)

    # Only this composition root selects the concrete practice destination.
    with sigma_practice_destination(data_dir, port) as destination:
        yield LocalPracticeWorker(db, destination, policy, open_reasoning)
