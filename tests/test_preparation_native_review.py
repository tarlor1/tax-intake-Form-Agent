"""Native review lifecycle with synthetic targets and a network-denying firm transport."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from src.preparation.native_review import HttpReviewReservations, NativeReview
from src.preparation.review_events import ReviewAccess, ReviewUnavailable
from src.preparation.session_lock import SessionBusy, hold_session
from src.preparation.sigma_config import SigmaConfig
from src.review_store import ReviewError


ACCESS = ReviewAccess("test-firm", "test-preparer", "test-return")
CONFIG = SigmaConfig(schema_version=1, firm_id=ACCESS.firm_id, machine_id="test-machine",
                     service_origin="https://firm.invalid", product="web", tax_year=2025, profile_id="test-profile")
REVISION = "r1"
REVIEW = "central-review-id"
DRAFT = "synthetic-draft"
ADDRESS = (DRAFT, "w2", "employer-a", "wages")
BASE = f"/api/sigma/v1/returns/{ACCESS.return_id}/review/reservations"


class PracticeFirm:
    def __init__(self):
        self.state = "queued"
        self.expires = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.requests = []
        self.checkout = False
        self.lost_activate = False
        self.lost_close = False
        self.exclude_binding = False
        self.reply_profile = CONFIG.profile_id
        self.activation_proofs = 0
        self.exit_proofs = 0

    def reservation(self, status=None):
        claimed = self.state != "queued"
        return {
            "reviewId": REVIEW, "firmId": ACCESS.firm_id, "preparerId": ACCESS.preparer_id,
            "returnId": ACCESS.return_id, "machineId": CONFIG.machine_id, "profileId": self.reply_profile,
            "inputRevision": REVISION, "product": CONFIG.product, "status": status or self.state,
            "generation": 7 if claimed else None, "draftKey": DRAFT if claimed and self.state != "closed" else None,
            "expiresAt": self.expires.isoformat() if claimed else None, "clientKey": "SYNTHETIC-CLIENT",
            "taxYear": CONFIG.tax_year, "returnType": "individual", "bindings": [] if self.exclude_binding else [
                {"section": "w2", "record": "employer-a", "field": "wages", "bindingId": "binding-a"},
                {"section": "w2", "record": "employer-b", "field": "wages", "bindingId": "binding-b"},
            ],
        }

    def handle(self, request):
        assert request.url.host == "firm.invalid"
        body = json.loads(request.content) if request.content else None
        action = (request.method, request.url.path)
        self.requests.append((*action, body))
        member = f"{BASE}/{REVIEW}"
        if action == ("POST", BASE):
            assert body in ({"inputRevision": REVISION}, {"inputRevision": REVISION, "queueOnly": True})
            if self.state in {"reserved", "active"}:
                return httpx.Response(200, json=self.reservation("recovery_required"))
            assert self.state == "queued"
            if not body.get("queueOnly"):
                self.state = "reserved"
        elif action == ("GET", member):
            assert body is None
        elif action == ("POST", f"{member}/activate"):
            assert self.state == "reserved" and self.checkout
            assert body == {"generation": 7, "proofId": "practice-checkout-confirmed"}
            self.state = "active"
            if self.lost_activate:
                return httpx.Response(503)
        elif action == ("POST", f"{member}/close"):
            assert not self.checkout
            assert body == {"generation": 7, "proofId": "practice-exit-confirmed"}
            self.state = "closed"
            if self.lost_close:
                return httpx.Response(503)
        else:
            pytest.fail(f"Unexpected request to the synthetic firm: {action}")
        return httpx.Response(200, json=self.reservation())

    async def activate_proof(self, grant, window):
        self.activation_proofs += 1
        assert (grant.review_id, grant.generation, grant.draft_key) == (REVIEW, 7, DRAFT)
        assert window.is_open() and self.checkout
        return "practice-checkout-confirmed"

    async def exit_proof(self, grant, _window):
        self.exit_proofs += 1
        assert (grant.review_id, grant.generation, grant.draft_key) == (REVIEW, 7, DRAFT)
        return None if self.checkout else "practice-exit-confirmed"

    @asynccontextmanager
    async def reservations(self):
        async with httpx.AsyncClient(base_url=CONFIG.service_origin, transport=httpx.MockTransport(self.handle),
                                     trust_env=False, follow_redirects=False) as client:
            async def request_json(method, path, **kwargs):
                response = await client.request(method, f"/api/sigma/v1{path}", **kwargs)
                if not response.is_success:
                    raise ReviewError(response.status_code, "Synthetic firm response lost.")
                return response.json()

            yield HttpReviewReservations(request_json)


class PracticeWindow:
    def __init__(self, firm):
        self.firm = firm
        self.open = True
        self.stopped = asyncio.Event()
        self.notify = None
        self.disposed = False

    def is_open(self):
        return self.open

    @asynccontextmanager
    async def observe(self, notify):
        assert self.firm.state == "active"
        self.notify = notify
        notify(ADDRESS)
        try:
            yield
        finally:
            self.stopped.set()

    async def dispose(self):
        assert not self.firm.checkout
        self.open = False
        self.disposed = True


class PracticeDriver:
    def __init__(self, firm, seat_path):
        self.firm = firm
        self.seat_path = seat_path
        self.windows = []

    async def open(self, reservation):
        assert self.firm.state == "reserved" and not self.firm.checkout
        assert reservation.identity.client_reference == "SYNTHETIC-CLIENT"
        _require_locked(self.seat_path)
        self.firm.checkout = True
        window = PracticeWindow(self.firm)
        self.windows.append(window)
        return window


def _require_locked(path):
    with pytest.raises(SessionBusy), hold_session(path):
        pytest.fail("Another writer obtained the exclusive interactive seat.")


def runtime(firm, driver, reservations):
    return NativeReview(CONFIG, reservations, driver, lock_path=driver.seat_path,
                        activation_proof=firm.activate_proof, exit_proof=firm.exit_proof)


def test_native_review_queues_then_opens_exactly_once_and_keeps_ownership_after_sse_disconnect(tmp_path):
    async def exercise():
        firm = PracticeFirm()
        driver = PracticeDriver(firm, tmp_path / "seat.lock")
        async with firm.reservations() as reservations:
            review = runtime(firm, driver, reservations)
            with hold_session(driver.seat_path):
                queued = await review.open(ACCESS, REVISION)
                assert (queued.status, queued.review_session_id) == ("queued", None)
                assert firm.requests[-1][-1] == {"inputRevision": REVISION, "queueOnly": True}
                assert not driver.windows

            opened = await review.open(ACCESS, REVISION)
            assert (opened.status, opened.review_session_id, opened.input_revision) == ("open", REVIEW, REVISION)
            assert await review.open(ACCESS, REVISION) == opened
            assert len(driver.windows) == 1 and firm.activation_proofs == 1
            window = driver.windows[0]
            session = review.session(ACCESS, REVIEW)
            with pytest.raises(ReviewUnavailable):
                review.session(ReviewAccess(ACCESS.firm_id, "other-preparer", ACCESS.return_id), REVIEW)
            # Reaching the loop after subscribing delivers the observer's initial
            # focus, followed by opaque row-specific field events in order.
            async with asyncio.timeout(2), session.subscribe(ACCESS, REVIEW) as events:
                selection = await anext(events)
                if selection.status == "detached":
                    selection = await anext(events)
                assert (selection.review_session_id, selection.binding_id) == (REVIEW, "binding-a")
                window.notify(ADDRESS)
                window.notify((DRAFT, "w2", "employer-b", "wages"))
                window.notify(("wrong-draft", "w2", "employer-a", "wages"))
                assert (await anext(events)).binding_id == "binding-b"
                assert (await anext(events)).status == "detached"
            assert firm.state == "active" and not window.disposed
            _require_locked(driver.seat_path)
            with pytest.raises(ReviewError, match="Exit the exact draft"):
                await review.close(ACCESS, REVIEW)
            assert firm.state == "active" and not window.disposed

            firm.checkout = False
            await review.close(ACCESS, REVIEW)
            assert firm.state == "closed" and window.disposed and window.stopped.is_set()
            with hold_session(driver.seat_path):
                pass
            with pytest.raises(ReviewUnavailable):
                review.session(ACCESS, REVIEW)

    asyncio.run(exercise())


def test_expiry_stops_observation_but_retains_owner_until_proven_exit(tmp_path):
    async def exercise():
        firm = PracticeFirm()
        firm.expires = datetime.now(timezone.utc) + timedelta(seconds=1)
        driver = PracticeDriver(firm, tmp_path / "seat.lock")
        async with firm.reservations() as reservations:
            review = runtime(firm, driver, reservations)
            assert (await review.open(ACCESS, REVISION)).status == "open"
            window = driver.windows[0]
            await asyncio.wait_for(window.stopped.wait(), 5)
            assert firm.state == "active" and window.is_open() and not window.disposed
            _require_locked(driver.seat_path)
            with pytest.raises(ReviewUnavailable):
                review.session(ACCESS, REVIEW)
            assert (await review.open(ACCESS, REVISION)).status == "recovery_required"
            firm.checkout = False
            await review.close(ACCESS, REVIEW)
            assert firm.state == "closed"

    asyncio.run(exercise())


def test_restart_returns_same_expired_central_owner_and_releases_only_with_independent_proof(tmp_path):
    async def exercise():
        firm = PracticeFirm()
        firm.state, firm.checkout = "active", True
        firm.expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        driver = PracticeDriver(firm, tmp_path / "seat.lock")
        async with firm.reservations() as reservations:
            review = runtime(firm, driver, reservations)
            opened = await review.open(ACCESS, REVISION)
            assert (opened.status, opened.review_session_id) == ("recovery_required", REVIEW)
            assert not driver.windows
            with pytest.raises(ReviewUnavailable):
                review.session(ACCESS, REVIEW)
            with pytest.raises(ReviewError, match="Exit the exact draft"):
                await review.close(ACCESS, REVIEW)
            assert firm.state == "active" and firm.exit_proofs == 1
            firm.checkout = False
            await review.close(ACCESS, REVIEW)
            assert firm.state == "closed" and firm.exit_proofs == 2
            await review.close(ACCESS, REVIEW)
            assert not driver.windows and firm.exit_proofs == 2

    asyncio.run(exercise())


def test_ambiguous_activation_and_close_responses_do_not_repeat_native_launch_or_release_early(tmp_path):
    async def exercise():
        firm = PracticeFirm()
        firm.lost_activate = firm.lost_close = True
        driver = PracticeDriver(firm, tmp_path / "seat.lock")
        async with firm.reservations() as reservations:
            review = runtime(firm, driver, reservations)
            opened = await review.open(ACCESS, REVISION)
            assert (opened.status, opened.review_session_id) == ("recovery_required", REVIEW)
            assert firm.state == "active" and len(driver.windows) == 1
            assert await review.open(ACCESS, REVISION) == opened
            _require_locked(driver.seat_path)
            firm.checkout = False
            with pytest.raises(ReviewError, match="response lost"):
                await review.close(ACCESS, REVIEW)
            assert firm.state == "closed"
            _require_locked(driver.seat_path)
            await review.close(ACCESS, REVIEW)
            with hold_session(driver.seat_path):
                pass
            assert len(driver.windows) == 1 and driver.windows[0].disposed
            assert firm.activation_proofs == firm.exit_proofs == 1

    asyncio.run(exercise())


def test_mismatched_profile_cannot_launch_native_window(tmp_path):
    async def exercise():
        firm = PracticeFirm()
        firm.reply_profile = "different-enrolled-profile"
        driver = PracticeDriver(firm, tmp_path / "seat.lock")
        async with firm.reservations() as reservations:
            review = runtime(firm, driver, reservations)
            with pytest.raises(ReviewError, match="not found"):
                await review.open(ACCESS, REVISION)
            assert not driver.windows and firm.activation_proofs == 0
            with hold_session(driver.seat_path):
                pass

    asyncio.run(exercise())
