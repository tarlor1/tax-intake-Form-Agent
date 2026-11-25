"""Outbound port for preparing a previously analyzed inbox original."""

from __future__ import annotations

from typing import Protocol

from .batches import BatchView, IngestScope
from .dispatch import ExtractionPolicy


class InboxPreparation(Protocol):
    recipient: ExtractionPolicy
    approval_label: str
    description: str

    def dispatch_one(self, scope: IngestScope) -> bool:
        """Schedule one approved, analyzed original for preparation."""
        ...

    def approve(self, scope: IngestScope, batch_id: str, slot_id: str) -> None: ...

    def annotate(self, scope: IngestScope, batches: tuple[BatchView, ...]) -> tuple[BatchView, ...]: ...

    async def run_once(self) -> str | None: ...
