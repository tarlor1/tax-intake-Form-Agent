"""Local native review handoff; only the firm service can release a reservation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, async_playwright
from pydantic import AwareDatetime, Field, StrictInt, StringConstraints, TypeAdapter, ValidationError, model_validator

from ..review_store import ReviewError
from .contracts import Contract, Text
from .native_plan import DraftIdentity
from .review_events import NativeAddress, NativeSelection, ReviewAccess, ReviewSelectionSession, ReviewUnavailable
from .review_routes import ReviewOpened
from .session_lock import SessionBusy, hold_session
from .sigma_config import SigmaConfig
from .sigma_web import WebBlocked, WebProfile
from .tool_contract import Ref
from .web_observer import ReviewSection, WebSelectionObserver


class ReviewBinding(Contract):
    section: Ref
    record: Ref | None
    field: Ref
    binding_id: Ref

    @property
    def address(self) -> NativeAddress:
        return NativeAddress(self.section, self.record, self.field)


class ReviewReservation(Contract):
    review_id: Ref
    firm_id: Ref
    preparer_id: Ref
    return_id: Ref
    machine_id: Ref
    profile_id: Ref
    input_revision: Ref
    product: Literal["web", "desktop"]
    status: Literal["queued", "reserved", "active", "recovery_required", "closed", "cancelled"]
    generation: Annotated[StrictInt, Field(ge=1)] | None
    draft_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)] | None
    expires_at: AwareDatetime | None
    client_key: Text
    tax_year: Annotated[StrictInt, Field(ge=1900, le=2200)]
    return_type: Literal["individual", "business", "1120"]
    bindings: tuple[ReviewBinding, ...]

    @model_validator(mode="after")
    def validate_reservation(self):
        if self.status in {"reserved", "active", "recovery_required"} and (
            self.generation is None or self.draft_key is None or self.expires_at is None
        ):
            raise ValueError("A claimed review must identify its draft, owner generation and evidence expiry.")
        if self.status in {"queued", "cancelled"} and self.generation is not None:
            raise ValueError("An unclaimed review cannot have an owner generation.")
        if (len({binding.address for binding in self.bindings}) != len(self.bindings)
                or len({binding.binding_id for binding in self.bindings}) != len(self.bindings)):
            raise ValueError("The native binding map is ambiguous.")
        return self

    @property
    def access(self) -> ReviewAccess:
        return ReviewAccess(self.firm_id, self.preparer_id, self.return_id)

    @property
    def identity(self) -> DraftIdentity:
        return DraftIdentity(client_reference=self.client_key, tax_year=self.tax_year, return_type=self.return_type)

    def same_owner(self, other: ReviewReservation) -> bool:
        return (self.review_id, self.input_revision, self.generation, self.draft_key,
                self.client_key, self.tax_year, self.return_type) == (
                    other.review_id, other.input_revision, other.generation, other.draft_key,
                    other.client_key, other.tax_year, other.return_type,
                ) and (self.access, self.machine_id, self.profile_id, self.product) == (
                    other.access, other.machine_id, other.profile_id, other.product,
                )

class ReviewReservations(Protocol):
    async def request(self, access: ReviewAccess, revision: str, *, queue_only: bool) -> ReviewReservation: ...
    async def get(self, access: ReviewAccess, review_id: str) -> ReviewReservation: ...
    async def activate(self, reservation: ReviewReservation, proof_id: str) -> ReviewReservation: ...
    async def close(self, reservation: ReviewReservation, proof_id: str) -> ReviewReservation: ...


class RequestJSON(Protocol):
    async def __call__(self, method: str, path: str, **kwargs) -> object: ...


_REF = TypeAdapter(Ref)


def _safe_ref(value: str) -> str:
    if value in {".", ".."}:
        raise ValueError("Invalid review reference.")
    return _REF.validate_python(value)


class HttpReviewReservations:
    """Use the workspace's authenticated, fixed-origin JSON client and enrollment headers."""

    def __init__(self, request_json: RequestJSON):
        self._request_json = request_json

    @staticmethod
    def _base(access: ReviewAccess) -> str:
        try:
            return f"/returns/{_safe_ref(access.return_id)}/review/reservations"
        except ValueError:
            raise ReviewError(404, "Return not found in this enrollment.") from None

    @staticmethod
    def _member(access: ReviewAccess, review_id: str) -> str:
        try:
            return f"{HttpReviewReservations._base(access)}/{_safe_ref(review_id)}"
        except ValueError:
            raise ReviewError(404, "Review reservation not found in this return.") from None

    async def _request(self, method: str, path: str, **kwargs) -> ReviewReservation:
        reply = await self._request_json(method, path, **kwargs)
        try:
            return ReviewReservation.model_validate(reply)
        except ValidationError:
            raise ReviewError(502, "The firm returned an invalid native review reservation.") from None

    async def request(self, access: ReviewAccess, revision: str, *, queue_only: bool) -> ReviewReservation:
        body = {"inputRevision": revision}
        if queue_only:
            body["queueOnly"] = True
        return await self._request("POST", self._base(access), json=body)

    async def get(self, access: ReviewAccess, review_id: str) -> ReviewReservation:
        return await self._request("GET", self._member(access, review_id))

    async def _with_proof(self, action: str, reservation: ReviewReservation, proof_id: str) -> ReviewReservation:
        if reservation.generation is None:
            raise ReviewError(409, "There is no native owner to confirm.")
        return await self._request(
            "POST", f"{self._member(reservation.access, reservation.review_id)}/{action}",
            json={"generation": reservation.generation, "proofId": proof_id},
        )

    async def activate(self, reservation: ReviewReservation, proof_id: str) -> ReviewReservation:
        return await self._with_proof("activate", reservation, proof_id)

    async def close(self, reservation: ReviewReservation, proof_id: str) -> ReviewReservation:
        return await self._with_proof("close", reservation, proof_id)


NotifySelection = Callable[[NativeSelection | None], None]


class NativeWindow(Protocol):
    def is_open(self) -> bool:
        """Local display state only, never proof of native ownership release."""
        ...

    def observe(self, notify: NotifySelection) -> AbstractAsyncContextManager[None]:
        """Subscribe before reading initial selection; emit only verified opaque context."""
        ...

    async def dispose(self) -> None:
        """Clean up resources owned by this launcher after independent exit proof."""
        ...


class ReviewDriver(Protocol):
    async def open(self, reservation: ReviewReservation) -> NativeWindow:
        """Open and verify only the exact unfiled draft in a separate approved native session."""
        ...


# These trusted callbacks obtain identifiers from the firm's independent native
# authority. The authority verifies them against the exact generation at the
# service. ExitProof must work even after restart (when there is no local handle).
ActivationProof = Callable[[ReviewReservation, NativeWindow], Awaitable[str]]
ExitProof = Callable[[ReviewReservation, NativeWindow | None], Awaitable[str | None]]


@dataclass(slots=True)
class _LocalReview:
    reservation: ReviewReservation
    seat: ExitStack
    window: NativeWindow | None = None
    session: ReviewSelectionSession | None = None
    observer: AsyncExitStack | None = None
    expiry: asyncio.TimerHandle | None = None


class NativeReview:
    """Compose an exact central reservation, local seat, human window and evidence stream.

    A desktop deployment supplies a licensed ReviewDriver and the same trusted
    proof callbacks. Closing an SSE subscriber, an expired token, observer loss
    and companion restart have no authority to release the native return.
    """

    def __init__(
        self, config: SigmaConfig, reservations: ReviewReservations, driver: ReviewDriver, *,
        lock_path: Path, activation_proof: ActivationProof, exit_proof: ExitProof,
    ) -> None:
        if not lock_path.is_absolute() or not lock_path.parent.is_dir():
            raise ValueError("Native review requires the provisioned absolute local seat lock path.")
        self._config = config
        self._reservations = reservations
        self._driver = driver
        self._lock_path = lock_path
        self._activation_proof = activation_proof
        self._exit_proof = exit_proof
        self._lifecycle = asyncio.Lock()
        self._local: _LocalReview | None = None
        self._expiry_task: asyncio.Task[None] | None = None

    def _scope(self, access: ReviewAccess, reservation: ReviewReservation, *,
               revision: str | None = None, review_id: str | None = None) -> None:
        config = self._config
        if (reservation.access != access or access.firm_id != config.firm_id
                or reservation.machine_id != config.machine_id or reservation.profile_id != config.profile_id
                or reservation.product != config.product or reservation.tax_year != config.tax_year
                or (revision is not None and reservation.input_revision != revision)
                or (review_id is not None and reservation.review_id != review_id)):
            raise ReviewError(404, "Review reservation not found in this return and enrollment.")

    @staticmethod
    def _opened(reservation: ReviewReservation, status: Literal["open", "queued", "recovery_required"]) -> ReviewOpened:
        return ReviewOpened(status=status, review_session_id=None if status == "queued" else reservation.review_id,
                            return_id=reservation.return_id, input_revision=reservation.input_revision)

    @staticmethod
    def _proof(proof_id: str | None) -> str:
        try:
            return _REF.validate_python(proof_id)
        except ValidationError:
            raise ReviewError(409, "The firm has not confirmed the exact native action for this review.") from None

    async def open(self, access: ReviewAccess, revision: str) -> ReviewOpened:
        async with self._lifecycle:
            held = self._local
            if held is not None and held.reservation.access == access:
                if (held.session is not None and held.session.active and held.window is not None
                        and held.window.is_open()):
                    if held.reservation.input_revision != revision:
                        raise ReviewError(409, "Close the active native review before opening another revision.")
                    return self._opened(held.reservation, "open")
                return self._opened(held.reservation, "recovery_required")
            with ExitStack() as seat:
                available = False
                if held is None:
                    try:
                        seat.enter_context(hold_session(self._lock_path))
                        available = True
                    except SessionBusy:
                        pass  # The central queue can record review priority without claiming the seat.
                reservation = await self._reservations.request(access, revision, queue_only=not available)
                self._scope(access, reservation, revision=None if reservation.status == "recovery_required" else revision)
                if reservation.status == "queued":
                    return self._opened(reservation, "queued")
                if reservation.status in {"active", "recovery_required"}:
                    # An existing owner, or a lost response to a prior claim, is
                    # recoverable by exact closure. Never open a second window.
                    return self._opened(reservation, "recovery_required")
                if reservation.status != "reserved" or not available:
                    raise ReviewError(409, "A new native review was not reserved for this seat.")
                local = self._local = _LocalReview(reservation, seat.pop_all())
            return await self._open_reserved(local)

    async def _open_reserved(self, local: _LocalReview) -> ReviewOpened:
        reservation = local.reservation
        try:
            if reservation.expires_at is None or reservation.expires_at <= datetime.now(timezone.utc):
                raise ReviewUnavailable("The native review grant has expired.")
            async with asyncio.timeout((reservation.expires_at - datetime.now(timezone.utc)).total_seconds()):
                window = local.window = await self._driver.open(reservation)
                proof = self._proof(await self._activation_proof(reservation, window))
                active = await self._reservations.activate(reservation, proof)
            if (active.status != "active" or not reservation.same_owner(active)
                    or active.draft_key is None or active.expires_at is None or not window.is_open()):
                raise ReviewUnavailable("The exact native activation was not confirmed.")
            local.reservation = active
            session = ReviewSelectionSession(
                active.access, session_id=active.review_id, input_revision=active.input_revision,
                draft_key=active.draft_key, expires_at=active.expires_at,
                bindings={binding.address: binding.binding_id for binding in active.bindings},
            )
            loop = asyncio.get_running_loop()

            def forward(selection: NativeSelection | None) -> None:
                # Desktop adapters may notify on their UIA event thread. Web
                # observers have the same ordered callback on the local loop.
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(session.native_selection, selection)

            async with AsyncExitStack() as observer:
                async with asyncio.timeout(max(0, (active.expires_at - datetime.now(timezone.utc)).total_seconds())):
                    await observer.enter_async_context(window.observe(forward))
                if not session.active or not window.is_open():
                    raise ReviewUnavailable("The native window or grant was lost during activation.")
                local.expiry = loop.call_later(
                    max(0, (active.expires_at - datetime.now(timezone.utc)).total_seconds()), self._expire, local,
                )
                local.session = session
                local.observer = observer.pop_all()
            return self._opened(active, "open")
        except Exception:
            # A failed or unacknowledged step is not permission to retry the
            # native launch or free the seat. Preserve this exact review for close.
            return self._opened(reservation, "recovery_required")

    def _expire(self, local: _LocalReview) -> None:
        self._expiry_task = asyncio.create_task(self._expire_observation(local))

    async def _expire_observation(self, local: _LocalReview) -> None:
        async with self._lifecycle:
            if self._local is local:
                await self._stop_observing(local)

    @staticmethod
    async def _stop_observing(local: _LocalReview) -> None:
        if local.expiry is not None:
            local.expiry.cancel()
            local.expiry = None
        if local.session is not None:
            local.session.close()
            local.session = None
        if local.observer is not None:
            await local.observer.aclose()
            local.observer = None

    def session(self, access: ReviewAccess, session_id: str) -> ReviewSelectionSession:
        local = self._local
        if local is None or local.session is None:
            raise ReviewUnavailable("Review observation is not available on this companion.")
        local.session.require_access(access, session_id)
        return local.session

    async def close(self, access: ReviewAccess, session_id: str) -> None:
        async with self._lifecycle:
            local = self._local
            if local is not None and (local.reservation.access, local.reservation.review_id) != (access, session_id):
                local = None
            current = await self._reservations.get(access, session_id)
            self._scope(access, current, review_id=session_id)
            if current.status == "closed" and local is None:
                return
            if current.status not in {"reserved", "active", "recovery_required", "closed"}:
                raise ReviewError(409, "This review does not own a native window.")
            if local is not None and current.status != "closed" and not local.reservation.same_owner(current):
                raise ReviewError(409, "The native review owner changed. Firm recovery is required.")
            if local is not None and current.status == "closed" and (
                current.generation != local.reservation.generation or
                current.input_revision != local.reservation.input_revision
            ):
                raise ReviewError(409, "The closed review does not match the local native owner.")
            with ExitStack() as recovery_seat:
                if local is None:
                    try:
                        recovery_seat.enter_context(hold_session(self._lock_path))
                    except SessionBusy:
                        raise ReviewError(409, "The interactive seat is busy. Firm recovery is required.") from None
                # A terminal reply after a lost close response can omit draft
                # details. If the local window remains, prove exit using its
                # saved scope before disposing any process or releasing the lock.
                reservation = local.reservation if current.status == "closed" and local is not None else current
                window = local.window if local is not None else None
                if current.status != "closed" or window is not None:
                    try:
                        proof_id = self._proof(await self._exit_proof(reservation, window))
                    except Exception:
                        raise ReviewError(409, "Exit the exact draft in Sigma and obtain confirmed native checkout release.") from None
                if local is not None:
                    try:
                        await self._stop_observing(local)
                        if local.window is not None:
                            await local.window.dispose()
                            local.window = None
                    except Exception:
                        raise ReviewError(409, "The reviewer window has not been safely released on this seat.") from None
                if current.status != "closed":
                    closed = await self._reservations.close(reservation, proof_id)
                    self._scope(access, closed, revision=reservation.input_revision, review_id=session_id)
                    if closed.status != "closed" or closed.generation != reservation.generation:
                        raise ReviewError(409, "The firm has not confirmed release of this native review.")
                if local is not None:
                    local.seat.close()
                    self._local = None


EnterWebDraft = Callable[[Page, ReviewReservation], Awaitable[None]]


class HeadedWebReview:
    """An approved Chromium reviewer profile, separate from the worker's browser.

    The trusted entry callback handles sign-in/MFA with the preparer directly in
    Sigma, exact return search, and opening only the existing authorized draft.
    It must return on the owned page with all temporary authentication tabs closed.
    Subsequent interactions are the preparer's; the only automation is read-only
    selection observation for an identified draft context.
    """

    def __init__(
        self, profile: WebProfile, *, user_data_dir: Path, enter: EnterWebDraft,
        sections: tuple[ReviewSection, ...], record_key_pattern: str, executable_path: Path | None = None,
    ) -> None:
        if not user_data_dir.is_absolute() or not user_data_dir.is_dir():
            raise ValueError("Provide a dedicated, pre-provisioned Chromium reviewer profile directory.")
        if executable_path is not None and (not executable_path.is_absolute() or not executable_path.is_file()):
            raise ValueError("The approved Chromium executable must be an absolute file path.")
        self._profile = profile
        self._user_data_dir = user_data_dir
        self._enter = enter
        self._sections = sections
        self._record_key_pattern = record_key_pattern
        self._executable_path = executable_path

    async def open(self, reservation: ReviewReservation) -> NativeWindow:
        profile = self._profile
        if (reservation.product != "web" or reservation.profile_id != profile.profile_id
                or reservation.draft_key is None or reservation.return_type not in profile.return_types):
            raise WebBlocked("The reservation does not match the approved Chromium reviewer profile.")
        async with AsyncExitStack() as cleanup:
            playwright = await cleanup.enter_async_context(async_playwright())
            context = await playwright.chromium.launch_persistent_context(
                self._user_data_dir, headless=False, executable_path=self._executable_path,
                accept_downloads=False, env={key: value for key, value in os.environ.items() if key.upper() in {
                    "APPDATA", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "HOME", "HOMEDRIVE", "HOMEPATH",
                    "LANG", "LC_ALL", "LOCALAPPDATA", "PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR",
                    "USERPROFILE", "WAYLAND_DISPLAY", "WINDIR", "XAUTHORITY", "XDG_RUNTIME_DIR",
                }},
            )
            cleanup.push_async_callback(context.close)
            pages = context.pages
            if not pages:
                page = await context.new_page()
            elif len(pages) == 1 and pages[0].url == "about:blank":
                page = pages[0]
            else:
                raise WebBlocked("A dedicated reviewer profile must start on one blank page.")
            page.set_default_timeout(profile.timeout_ms)
            page.set_default_navigation_timeout(profile.timeout_ms)
            await self._enter(page, reservation)
            await self._verify_entry(page, reservation)
            return _WebReviewWindow(page, context, profile, reservation, self._sections, self._record_key_pattern,
                                    cleanup.pop_all())

    async def _verify_entry(self, page: Page, reservation: ReviewReservation) -> None:
        profile = self._profile
        url = page.url
        split = urlsplit(url)
        route = profile._draft_route(split.path)
        if (f"{split.scheme}://{split.netloc}{split.path}" != url or
                f"{split.scheme}://{split.netloc}" != profile.origin or route is None
                or route.key != reservation.draft_key or page.context.pages != [page] or len(page.frames) != 1):
            raise WebBlocked("Native entry did not establish the exact authorized draft and window.")
        scope = profile.context
        state = await page.locator(scope.draft_root).evaluate_all("""(roots, config) => {
            if (window !== window.top || location.href !== config.url ||
                roots.length !== 1 || document.querySelector('base')) return null;
            const root = roots[0];
            if (!root.matches('main:not([role]),[role="main"]') || !root.getClientRects().length) return null;
            const dialogs = [...document.querySelectorAll('dialog[open],[role="dialog"]')]
                .filter(element => element.getClientRects().length);
            if (config.dialog ? dialogs.length !== 1 || !root.contains(dialogs[0]) : dialogs.length !== 0) return null;
            return config.attributes.map(name => root.getAttribute(name));
        }""", {"url": url, "attributes": (*scope.identity_attributes, scope.section_attribute),
               "dialog": route.section.dialog})
        if (state != [reservation.draft_key, reservation.client_key, str(reservation.tax_year), reservation.return_type,
                      scope.unfiled_value, route.section.context] or page.url != url
                or page.context.pages != [page] or len(page.frames) != 1):
            raise WebBlocked("Native entry could not verify an unfiled draft with the expected identity.")


@dataclass(slots=True)
class _WebReviewWindow:
    page: Page
    context: BrowserContext
    profile: WebProfile
    reservation: ReviewReservation
    sections: tuple[ReviewSection, ...]
    record_key_pattern: str
    cleanup: AsyncExitStack

    def is_open(self) -> bool:
        return not self.page.is_closed()

    @asynccontextmanager
    async def observe(self, notify: NotifySelection) -> AsyncIterator[None]:
        key = self.reservation.draft_key
        if key is None:
            raise WebBlocked("The review observation has no verified native draft.")
        exclusive = self.context.pages == [self.page]

        def invalidate(_page: Page) -> None:
            nonlocal exclusive
            exclusive = False
            notify(None)

        def selection(value: NativeSelection | None) -> None:
            notify(value if exclusive else None)

        self.context.on("page", invalidate)
        try:
            async with AsyncExitStack() as observer:
                try:
                    await observer.enter_async_context(WebSelectionObserver(
                        self.page, self.profile, self.reservation.identity, key, self.sections,
                        self.record_key_pattern, selection,
                    ))
                except WebBlocked:
                    notify(None)  # Manual evidence selection remains available.
                yield
        finally:
            self.context.remove_listener("page", invalidate)

    async def dispose(self) -> None:
        await self.cleanup.aclose()
