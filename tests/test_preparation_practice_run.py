"""Exercise source-based selection with the guarded browser on the practice form."""

import asyncio

import pytest

from src.sigma_demo.app import run_case


@pytest.mark.parametrize("case", ["wages", "rideshare", "mixed"])
def test_practice_documents_drive_saved_sections(coordinator_db, tmp_path, case):
    # The interactive runner also checks exact saved values and row identities,
    # durable evidence, and native saves against the selected sections.
    result = asyncio.run(run_case(coordinator_db, tmp_path, case, headless=True))
    assert result.state == ("needs_attention" if case == "mixed" else "ready_for_review")
