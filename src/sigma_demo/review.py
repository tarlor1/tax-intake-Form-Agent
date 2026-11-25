"""Wire the real evidence API and native observer to a disposable practice draft."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
import re
from secrets import compare_digest, token_urlsafe
from socket import socket
from uuid import uuid4

import httpx
import uvicorn
from playwright.async_api import Page, async_playwright

from src.preparation.batches import BatchManifest, BatchView, MediaType
from src.preparation.firm_api import Principal
from src.preparation.native_plan import DraftIdentity
from src.preparation.native_review import HeadedWebReview, HttpReviewReservations, NativeReview, NativeWindow, ReviewReservation
from src.preparation.ownership import OwnerGrant
from src.preparation.sigma_config import FirmWorkspace, IntakeSettings, ModelEnrollment, SigmaConfig
from src.preparation.sigma_web import WebBlocked, WebProfile
from src.preparation.site import FirmRuntime
from src.preparation.web_actions import WebLayout
from src.preparation.web_observer import ReviewSection
from src.review_api import _companion_app
from src.review_store import ReviewError

from .native import PracticeAuthority


PairedCheck = Callable[[Page, Page], Awaitable[None]]
UploadCheck = Callable[[Page, tuple[Path, ...]], Awaitable[None]]


class PracticePreparer:
    """A single in-process synthetic preparer; no site credentials or production transport."""

    def __init__(self, firm: str, return_id: str, seat: str, profile: str):
        self.principal = Principal(firm, 'practice-review', frozenset({return_id}), seat, profile)
        self.token = token_urlsafe(32)

    def authenticate(self, candidate: str, machine: str, profile: str) -> Principal | None:
        if compare_digest(candidate, self.token) and (machine, profile) == (
                self.principal.machine_id, self.principal.profile_id):
            return self.principal
        return None


class _PracticeWorkspace(FirmWorkspace):
    """Run the real firm routes in process, using the same scoped HTTP client methods."""

    def __init__(self, config, runtime, preparer, native_factory, sealed: asyncio.Future[BatchView] | None):
        super().__init__(config, native_factory=native_factory)
        self._firm_app = runtime.app()
        self._preparer = preparer
        self._sealed = sealed
        extraction = runtime.site.extraction
        self._external_extraction = (extraction.policy.identity.provider if extraction is not None
                                     and extraction.policy.identity.model != 'local-deterministic' else None)
        self._batch_id: str | None = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._firm_app), base_url=self._config.service_origin,
            trust_env=False, headers={
                'Authorization': f'Bearer {self._preparer.token}',
                'X-Sigma-Profile': self._config.profile_id,
                'X-Sigma-Machine': self._config.machine_id,
            },
        )
        return self

    async def intake_settings(self) -> IntakeSettings:
        settings = await super().intake_settings()
        if self._sealed is not None and settings.approval_label:
            message = 'Practice use only: drop files from the sample folder printed in the terminal.'
            approval = 'Authorize local processing of this batch of synthetic documents'
            if self._external_extraction:
                approval = f'Authorize sending these synthetic documents to {self._external_extraction}'
                message += ' The provider receives the numbered text of these generated samples. Never upload client files.'
            return settings.model_copy(update={'approval_label': approval, 'message': message})
        return settings

    async def jobs(self, return_id: str) -> list[dict]:
        jobs = await super().jobs(return_id)
        for job in jobs:
            issues = []
            for issue in job["issues"]:
                missing_box = re.fullmatch(r"missing-w2-box-([1-3])-(test-emp-[a-z])-([a-z]-[0-9]+)", issue)
                if missing_box:
                    box, employer, instance = missing_box.groups()
                    issue = f"Needs review: W-2 Box {box} for {employer.upper()} / {instance.upper()} has no verified value."
                issues.append(issue)
            job["issues"] = issues
        return jobs

    async def declare(self, manifest: BatchManifest, return_id: str | None = None) -> BatchView:
        if self._sealed is not None:
            if return_id is not None or manifest.return_hint is not None:
                raise ReviewError(409, 'Use the general document inbox for this practice upload.')
            if self._batch_id is not None and self._batch_id != manifest.batch_id:
                raise ReviewError(409, 'This demo accepts one drop. Restart for another batch.')
            self._batch_id = manifest.batch_id
        return await super().declare(manifest, return_id)

    def _observe_seal(self, batch: BatchView) -> BatchView:
        if (self._sealed is not None and not self._sealed.done()
                and self._batch_id == batch.batch_id and batch.state != 'incomplete'):
            self._sealed.set_result(batch)
        return batch

    async def upload(
        self, batch_id: str, slot: str, media_type: MediaType, content: bytes, return_id: str | None = None,
    ) -> BatchView:
        return self._observe_seal(await super().upload(batch_id, slot, media_type, content, return_id))

    async def exclude(self, batch_id: str, slot: str) -> BatchView:
        return self._observe_seal(await super().exclude(batch_id, slot))


@asynccontextmanager
async def _serve(backend: FirmWorkspace):
    with socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        app = _companion_app(None, port=port, token=None, backend=backend)
        ready = asyncio.Event()

        class Server(uvicorn.Server):
            def capture_signals(self):
                return nullcontext()  # The enclosing asyncio.run owns keyboard interruption.

            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                ready.set()

        server = Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning',
                                       proxy_headers=False, timeout_graceful_shutdown=5))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            if not server.started:
                raise RuntimeError('The practice evidence companion did not start.')
            yield f'http://127.0.0.1:{port}'
        finally:
            server.should_exit = True
            await task


async def _local_requests(page: Page, origin: str) -> None:
    async def route_request(route):
        if route.request.url.startswith(f'{origin}/'):
            await route.continue_()
        else:
            await route.abort()

    await page.context.route('**/*', route_request)


@dataclass
class PracticeCompanion:
    page: Page
    origin: str
    sealed: asyncio.Future[BatchView] | None
    closed: asyncio.Future[None]
    native_page: Page | None = None
    saved: tuple[str, str] | None = None

    async def wait_for_batch(self) -> BatchView:
        if self.sealed is None:
            raise RuntimeError('The practice inbox was not opened for uploads.')
        done, _ = await asyncio.wait((self.sealed, self.closed), return_when=asyncio.FIRST_COMPLETED)
        if self.closed in done:
            raise RuntimeError('The companion closed before the practice upload could be processed.')
        return self.sealed.result()

    async def wait_closed(self) -> None:
        await self.closed

    async def show_review(self, client_id: str, tax_year: int, check: PairedCheck | None) -> None:
        url = f'{self.origin}/clients/{client_id}/returns/{tax_year}'
        # The inbox invalidates its queue on PUT acknowledgement, before the
        # worker finishes. Load the final projection once from the saved server state.
        await self.page.goto(url)
        button = self.page.get_by_role('button', name='Open review', exact=True)
        await button.wait_for()
        if not await button.is_enabled():
            raise RuntimeError('The saved practice draft is not available for native review.')
        print(f'Evidence companion: {url}', flush=True)
        print('Click Open review to open the saved synthetic draft in a separate window. '
              'Focus a field in the draft to follow its persisted evidence. '
              'Close the draft window before choosing Release review.', flush=True)
        if check is not None:
            await button.click()
            await self.page.get_by_text('Use the separate Sigma window to check and make edits.').wait_for()
            if self.native_page is None:
                raise RuntimeError('The companion did not open a separate practice draft window.')
            await check(self.page, self.native_page)


@asynccontextmanager
async def practice_companion(
    *, runtime: FirmRuntime, preparer: PracticePreparer, authority: PracticeAuthority,
    work: Path, profile: WebProfile, layout: WebLayout, identity: DraftIdentity,
    chromium: str | None, accept_upload: bool = False,
):
    principal = preparer.principal
    (return_id,) = principal.return_ids
    sealed: asyncio.Future[BatchView] | None = None
    extraction = None
    if accept_upload:
        if runtime.site.extraction is None:
            raise RuntimeError('The interactive practice inbox requires an enrolled extraction policy.')
        policy = runtime.site.extraction.policy
        provider = policy.identity
        extraction = ModelEnrollment(
            provider=provider.provider, model=provider.model, endpoint=provider.endpoint, account=provider.account,
            classes=policy.document_classes, max_requests=policy.max_requests, max_request_bytes=policy.max_request_bytes,
            max_seconds=int(policy.timeout_seconds), max_output_tokens=policy.output_limit,
            max_billable_input_tokens=policy.max_input_tokens, max_billable_output_tokens=policy.max_output_tokens,
            input_price_per_million=policy.input_price_per_million, output_price_per_million=policy.output_price_per_million,
            max_spend=policy.max_spend,
        )
        sealed = asyncio.get_running_loop().create_future()
    config = SigmaConfig(schema_version=1, firm_id=principal.firm_id, machine_id=principal.machine_id,
                         service_origin='https://practice-firm.invalid', product='web',
                         tax_year=identity.tax_year, profile_id=profile.profile_id, extraction=extraction)
    reviewer_profile = work / 'reviewer-browser'
    reviewer_profile.mkdir(mode=0o700)
    sections = tuple(ReviewSection(section.key, tuple(
        (f'input[name="{field.name}"]', field.key) for field in section.fields
    ), active_record='form[data-record-id]' if section.records else None) for section in layout.sections)
    companion: PracticeCompanion
    opened: tuple[ReviewReservation, NativeWindow] | None = None
    native: NativeReview

    async def enter(page: Page, reservation: ReviewReservation) -> None:
        if companion.saved is None:
            raise WebBlocked('No verified synthetic draft is ready for review.')
        draft_key, landing = companion.saved
        if (reservation.firm_id != principal.firm_id or reservation.identity != identity
                or reservation.draft_key != draft_key or reservation.return_id != return_id):
            raise WebBlocked('The synthetic reviewer was not assigned this saved draft.')
        await _local_requests(page, profile.origin)
        await page.goto(f'{profile.origin}/drafts/{draft_key}/{landing}')
        companion.native_page = page  # HeadedWebReview independently checks the exact identity.

    def owner(reservation: ReviewReservation) -> OwnerGrant:
        if reservation.generation is None:
            raise WebBlocked('No synthetic owner generation was reserved.')
        return OwnerGrant(reservation.firm_id, reservation.return_id, reservation.generation,
                          reservation.machine_id, reservation.preparer_id, None, reservation.review_id)

    async def activate(reservation: ReviewReservation, window: NativeWindow) -> str:
        nonlocal opened
        opened = reservation, window
        if not window.is_open():
            raise WebBlocked('The verified practice window is no longer open.')
        proof = uuid4().hex
        authority.checkouts.add((owner(reservation), reservation.draft_key, proof))
        return proof

    async def exit_proof(reservation: ReviewReservation, window: NativeWindow | None) -> str | None:
        if (opened is None or window is None or opened[1] is not window
                or not opened[0].same_owner(reservation) or window.is_open()):
            return None
        grant = owner(reservation)
        proof = uuid4().hex
        authority.checkouts = {entry for entry in authority.checkouts if entry[0] != grant}
        authority.closes.add((grant, reservation.draft_key, proof))
        return proof

    driver = HeadedWebReview(profile, user_data_dir=reviewer_profile, enter=enter, sections=sections,
                             record_key_pattern=r'[a-f0-9]{32}',
                             executable_path=Path(chromium).absolute() if chromium else None)

    def native_factory(config, request_json):
        nonlocal native
        native = NativeReview(config, HttpReviewReservations(request_json), driver, lock_path=work / 'seat.lock',
                              activation_proof=activate, exit_proof=exit_proof)
        return native

    backend = _PracticeWorkspace(config, runtime, preparer, native_factory, sealed)
    async with _serve(backend) as origin, async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=chromium, headless=False)
        try:
            evidence = await browser.new_page(accept_downloads=False, service_workers='block')
            await _local_requests(evidence, origin)
            closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            def on_close(_page: Page) -> None:
                if not closed.done():
                    closed.set_result(None)

            evidence.once('close', on_close)
            companion = PracticeCompanion(evidence, origin, sealed, closed)
            if accept_upload:
                await evidence.goto(f'{origin}/clients')
            yield companion
        finally:
            try:
                if opened is not None:
                    reservation, window = opened
                    # Shutdown closes this run's disposable browser only. Normal review release
                    # requires closing the draft window first and never forces a native close.
                    if window.is_open():
                        await window.dispose()
                    await native.close(reservation.access, reservation.review_id)
            finally:
                await browser.close()


async def paired_review(
    *, runtime: FirmRuntime, preparer: PracticePreparer, authority: PracticeAuthority,
    work: Path, profile: WebProfile, layout: WebLayout, identity: DraftIdentity,
    draft_key: str, landing: str, client_id: str, chromium: str | None,
    pause: Callable[[], Awaitable[None]], check: PairedCheck | None = None,
) -> None:
    async with practice_companion(runtime=runtime, preparer=preparer, authority=authority, work=work,
                                  profile=profile, layout=layout, identity=identity, chromium=chromium) as companion:
        companion.saved = draft_key, landing
        await companion.show_review(client_id, identity.tax_year, check)
        if check is None:
            await pause()
