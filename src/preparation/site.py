"""Compose a firm-local coordinator with explicitly installed integration adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
import logging
import re
from typing import TYPE_CHECKING

from .agent import DataClass, StagePolicy
from .artifacts import DocumentVerifier, VerifiedArtifacts
from .batches import BatchIntake, ClassifiedUpload, CommittedBatchSource, ImmutableObjects
from .coordinator_db import CoordinatorDB, require_current_schema
from .dispatch import ExtractionPolicy, UploadCommitted, UploadDispatch
from .ownership import NativeAuthority, OwnerGrant, OwnershipBlocked, OwnershipStore
from .tax_store import ApprovedSum, DerivationAddress, PostgresTaxStore
from .upload_processing import UploadProcessor
from .worker import OpenProvider, PreparationWorker, WorkerEnrollment

if TYPE_CHECKING:
    from .firm_api import Principal

_LOG = logging.getLogger(__name__)


def load_site_factory(target: str) -> Callable:
    """Load a deployment-installed callable named by the operator, never by a request."""
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", target, re.ASCII):
        raise ValueError("Name a trusted installed factory as MODULE:CALLABLE.")
    module, attribute = target.split(":")
    try:
        factory = getattr(import_module(module), attribute)
    except (ImportError, AttributeError):
        raise ValueError("Could not load the named firm integration factory.") from None
    if not callable(factory):
        raise ValueError("The installed firm integration factory is not callable.")
    return factory


@dataclass(frozen=True)
class ExtractionEnrollment:
    policy: StagePolicy
    data_class: DataClass
    open_provider: OpenProvider

    @property
    def recipient(self) -> ExtractionPolicy:
        identity = self.policy.identity
        return ExtractionPolicy(identity.provider, identity.account, identity.model, self.data_class, identity.endpoint)


@dataclass(frozen=True)
class FirmSite:
    """Firm-managed identities, immutable storage, and independent source/native evidence."""

    firm_id: str
    authenticate: Callable[[str, str, str], Principal | None]
    native_authority: NativeAuthority
    objects: ImmutableObjects
    classify_upload: Callable[[UploadCommitted, str | None], ClassifiedUpload]
    verify_document: DocumentVerifier
    extraction: ExtractionEnrollment | None = None
    reasoning_policy: StagePolicy | None = None
    worker: WorkerEnrollment | None = None
    approved_sums: Mapping[DerivationAddress, ApprovedSum] = field(default_factory=dict)


class FirmRuntime:
    def __init__(self, db: CoordinatorDB, site: FirmSite):
        require_current_schema(db)
        if not site.firm_id or (site.worker is not None and site.worker.policy != site.reasoning_policy):
            raise ValueError("The enrolled worker must match this firm's pinned reasoning policy.")
        self.db, self.site = db, site
        self.intake = BatchIntake(db, site.objects)
        source = CommittedBatchSource(self.intake, site.classify_upload)
        self.dispatch = UploadDispatch(db, source, reasoning_policy=site.reasoning_policy)
        self.ownership = OwnershipStore(db, native_authority=site.native_authority)
        self.tax = PostgresTaxStore(db, approved_sums=site.approved_sums)
        extraction = site.extraction
        self.processor = (
            UploadProcessor(self.dispatch, source, VerifiedArtifacts(source, self.tax, site.verify_document),
                            extraction.policy, extraction.data_class, extraction.open_provider)
            if extraction else None
        )

    def app(self):
        from .firm_api import create_firm_app

        def authorize(token: str, machine: str, profile: str):
            principal = self.site.authenticate(token, machine, profile)
            return principal if principal is not None and principal.firm_id == self.site.firm_id else None

        return create_firm_app(
            authenticate=authorize, db=self.db, tax_store=self.tax, intake=self.intake, objects=self.site.objects,
            ownership=self.ownership, extraction_policy=self.site.extraction.recipient if self.site.extraction else None,
        )

    def _cordon_expired_workers(self) -> None:
        with self.db.transaction() as connection:
            rows = connection.execute("""
                SELECT firm_id,return_id,generation,machine_id,actor_id,job_id,review_id FROM prep_owners
                WHERE firm_id=%s AND job_id IS NOT NULL AND released_at IS NULL AND NOT reconciling
                  AND lease_until <= clock_timestamp()
            """, (self.site.firm_id,)).fetchall()
        for row in rows:
            try:
                self.ownership.interrupt(OwnerGrant(**row), "expired")
            except OwnershipBlocked:
                continue  # An independent process already released or renewed this generation.

    async def dispatch_once(self, *, concurrency: int = 4) -> int:
        """Replay committed local uploads and settle attempts; no browser or model fallback."""
        if not 1 <= concurrency <= 16:
            raise ValueError("Extraction concurrency must be between 1 and 16.")
        firm = self.site.firm_id
        await asyncio.to_thread(self._cordon_expired_workers)
        recovered = await asyncio.to_thread(self.dispatch.recover_extractions, firm_id=firm)
        events = await asyncio.to_thread(self.intake.committed_events, firm_id=firm)
        slots = asyncio.Semaphore(concurrency)

        pending = await asyncio.to_thread(self.dispatch.pending_extractions, firm_id=firm)
        published = set(events)

        async def process(event: UploadCommitted, *, publish: bool) -> int:
            async with slots:
                try:
                    if publish:
                        receipt = await asyncio.to_thread(self.dispatch.consume, event)
                        await asyncio.to_thread(self.intake.record_publication, event, received=receipt is not None)
                        if receipt is None:
                            return 0
                    if self.processor is not None:
                        disposition = await self.processor.handle(event)
                        return int(publish or disposition not in {"no-work", "uncommitted"})
                    settled = await asyncio.to_thread(self.dispatch.finish_without_extraction, event)
                    return int(publish or settled)
                except Exception as error:
                    # Keep the durable event/attempt and let unrelated returns proceed.
                    _LOG.warning("Upload processing interrupted (%s)", type(error).__name__)
                    return 0

        completed = await asyncio.gather(
            *(process(event, publish=True) for event in events),
            *(process(event, publish=False) for event in pending if event not in published),
        )
        return recovered + sum(completed)

    def worker(self) -> PreparationWorker:
        if self.site.worker is None:
            raise ValueError("No qualified interactive worker is enrolled by this integration.")
        return PreparationWorker(self.db, self.ownership, self.tax, firm_id=self.site.firm_id,
                                 enrollment=self.site.worker, approved_sums=self.site.approved_sums)
