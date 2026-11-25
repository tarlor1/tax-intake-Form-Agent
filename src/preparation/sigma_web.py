"""Bounded native navigation for an explicitly configured draft UI.

This module installs no licensed profile. It owns its Chromium process and a single
isolated context; the browser's network is open only during a declared UI transition.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import InitVar, dataclass, field
from ipaddress import IPv6Address, ip_address
from typing import Literal
from urllib.parse import SplitResult, parse_qsl, urljoin, urlsplit
from uuid import uuid4

from playwright.async_api import CDPSession, Error, Locator, Page, Response, Route, async_playwright
from pydantic import ConfigDict, StrictBool, StrictInt

from .native_plan import DraftIdentity


_TEST_ORIGIN = object()
_OWNED_BROWSER = object()


class WebBlocked(RuntimeError):
    """The UI or authorization did not establish the requested safe transition."""


@dataclass(frozen=True, slots=True)
class WebContext:
    __pydantic_config__ = ConfigDict(extra="forbid")

    list_heading: str
    search_form: str
    search_button: str
    search_labels: tuple[str, str, str]
    field_names: tuple[str, str, str]
    result_region: str
    open_link: str
    absent_status: str
    completion_attribute: str
    completion_value: str  # Approved positive proof of exact, exhaustive, unfiltered search.
    completion_text: str
    create_button: str
    draft_root: str
    identity_attributes: tuple[str, str, str, str, str]  # key, client, year, type, status
    unfiled_value: str
    section_attribute: str
    row_attribute: str


@dataclass(frozen=True, slots=True)
class WebSection:
    __pydantic_config__ = ConfigDict(extra="forbid")

    route: str  # value of the named `section` group in the route pattern
    context: str  # DOM section attribute value
    save_button: str
    rows: StrictBool = False
    dialog: StrictBool = False


@dataclass(frozen=True, slots=True)
class _DraftRoute:
    key: str
    section: WebSection
    row: str | None


def _path(path: str) -> bool:
    # The profile describes literal UI paths; decoded, normalized, or relative aliases
    # must never become additional allowlist entries.
    return bool(re.fullmatch(r"/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?", path))


@dataclass(frozen=True, slots=True)
class WebProfile:
    __pydantic_config__ = ConfigDict(extra="forbid")

    profile_id: str
    origin: str
    list_path: str
    create_path: str
    draft_pattern: str  # full-match expression with draft, section and row groups
    context: WebContext
    sections: tuple[WebSection, ...]
    landing_section: str
    return_types: tuple[str, ...]
    timeout_ms: StrictInt = 5_000
    _test_origin: InitVar[object | None] = field(default=None, kw_only=True)
    _draft_re: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self, _test_origin: object | None) -> None:
        origin = urlsplit(self.origin)
        hostname = origin.hostname or ""
        literal_loopback = hostname in {"127.0.0.1", "::1"}
        loopback = hostname.rstrip(".").split(".")[-1] == "localhost"
        if not loopback:
            try:
                address = ip_address(hostname)
                if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
                    address = address.ipv4_mapped
                loopback = address.is_loopback
            except ValueError:
                pass
        test_only = _test_origin is _TEST_ORIGIN
        # These must match Chromium's network origin, not a prefix or alias.
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = origin.port
        default_port = 443 if origin.scheme == "https" else 80
        address = f"{host}:{port}" if port is not None and port != default_port else host
        if (not host or self.origin != f"{origin.scheme}://{address}" or port == 0
                or (test_only and not (literal_loopback and origin.scheme == "http"))
                or (not test_only and (origin.scheme != "https" or loopback))):
            raise ValueError("Web profiles require an exact HTTPS origin or explicit HTTP loopback test origin.")
        if (not self.profile_id or not _path(self.list_path) or not _path(self.create_path)
                or self.list_path == self.create_path or not 100 <= self.timeout_ms <= 30_000):
            raise ValueError("Invalid web profile identity, paths, or timeout.")
        expression = re.compile(self.draft_pattern, re.ASCII)
        if set(expression.groupindex) != {"draft", "section", "row"}:
            raise ValueError("Draft paths must identify the exact draft, section, and optional row.")
        section_routes = {section.route for section in self.sections}
        if (len(section_routes) != len(self.sections) or self.landing_section not in section_routes
                or len({section.context for section in self.sections}) != len(self.sections)
                or not re.fullmatch(r"data-[a-z][a-z0-9-]*", self.context.completion_attribute)
                or not self.context.completion_value.strip() or not self.context.completion_text.strip()
                or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name) for name in self.context.field_names)
                or len(set(self.context.identity_attributes)) != 5
                or any(not re.fullmatch(r"data-[a-z][a-z0-9-]*", name) for name in (
                    *self.context.identity_attributes, self.context.section_attribute, self.context.row_attribute,
                ))
                or len(set(self.context.field_names)) != 3 or not self.return_types
                or expression.fullmatch(self.list_path) or expression.fullmatch(self.create_path)):
            raise ValueError("Ambiguous web profile context or routes.")
        object.__setattr__(self, "_draft_re", expression)

    @classmethod
    def for_test(cls, **settings) -> WebProfile:
        """Opt in to a dedicated ephemeral loopback fixture, never a remote origin."""
        return cls(**settings, _test_origin=_TEST_ORIGIN)

    def _draft_route(self, path: str) -> _DraftRoute | None:
        if not _path(path) or not (match := self._draft_re.fullmatch(path)):
            return None
        section = next((section for section in self.sections if section.route == match["section"]), None)
        if not match["draft"] or section is None or (match["row"] is not None and not section.rows):
            return None
        return _DraftRoute(match["draft"], section, match["row"])


@dataclass(frozen=True, slots=True)
class SearchResult:
    identity: DraftIdentity
    status: Literal["found", "absent"]
    draft_key: str | None
    proof: str


@dataclass(frozen=True, slots=True)
class CreatePermit:
    """Permit returned only by the trusted, ownership-scoped creation journal."""

    profile_id: str
    identity: DraftIdentity
    proof: str
    intent_id: str


@dataclass(frozen=True, slots=True)
class _Request:
    method: Literal["GET", "POST"]
    path: str | None = None
    query: tuple[tuple[str, str], ...] = ()
    form: tuple[tuple[str, str], ...] | None = None


@dataclass(slots=True)
class _Transition:
    requests: tuple[_Request, ...]
    seen: int = 0


def _fields(encoded: str, wanted: tuple[tuple[str, str], ...]) -> bool:
    if len(encoded) > 65_536:
        return False
    try:
        return sorted(parse_qsl(encoded, keep_blank_values=True, strict_parsing=True,
                                max_num_fields=len(wanted), errors="strict")) == sorted(wanted)
    except ValueError:
        return False


async def _one(locator: Locator) -> Locator:
    if await locator.count() != 1 or not await locator.is_visible():
        raise WebBlocked("missing-or-ambiguous-visible-control")
    return locator


class WebDraft:
    """One private Chromium page pinned to one exact return for its lifetime.

    The Page is available to the trusted field backend for DOM reads and checked
    input fills. Only this class can arm a native network transition. Caller code
    must not attach additional protocol sessions or weaken the browser configuration.
    """

    def __init__(self, profile: WebProfile, page: Page, cdp: CDPSession, frame_id: str, *, _owned: object) -> None:
        if _owned is not _OWNED_BROWSER:
            raise TypeError("WebDraft must launch its own dedicated Chromium browser.")
        self._profile = profile
        self._page = page
        self._cdp = cdp
        self._frame_id = frame_id
        self._lock = asyncio.Lock()
        self._identity: DraftIdentity | None = None
        self._key: str | None = None
        self._transition: _Transition | None = None
        self._violation: str | None = None
        self._create_intent: str | None = None

    @property
    def profile(self) -> WebProfile:
        return self._profile

    @property
    def page(self) -> Page:
        return self._page

    @classmethod
    @asynccontextmanager
    async def launch(
        cls, profile: WebProfile, *, executable_path: str | None = None, headed_practice: bool = False,
    ) -> AsyncIterator[WebDraft]:
        if headed_practice and not profile.origin.startswith("http://"):
            raise ValueError("Visible worker mode is only available for an explicit loopback practice profile.")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not headed_practice, executable_path=executable_path)
            try:
                context = await browser.new_context(
                    accept_downloads=False, java_script_enabled=False, service_workers="block",
                )
                page = await context.new_page()
                page.set_default_timeout(profile.timeout_ms)
                page.set_default_navigation_timeout(profile.timeout_ms)
                cdp = await context.new_cdp_session(page)
                frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]
                session = cls(profile, page, cdp, frame["id"], _owned=_OWNED_BROWSER)
                context.on("page", lambda _: session._block("unexpected-popup"))
                page.on("frameattached", lambda _: session._block("unexpected-child-frame"))
                page.on("framenavigated", session._frame_navigated)
                page.on("dialog", session._dismiss_dialog)
                page.on("download", session._reject_download)
                await context.route("**/*", session._route)
                cdp.on("Fetch.requestPaused", session._request_paused)
                # Playwright's route handler runs for the first request of a redirect only.
                # Chromium's Fetch interception checks every hop before sending it.
                await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
                yield session
            finally:
                await browser.close()

    def _block(self, reason: str) -> None:
        if self._violation is None:
            self._violation = reason

    async def _dismiss_dialog(self, dialog) -> None:
        self._block("unexpected-native-dialog")
        await dialog.dismiss()

    async def _reject_download(self, download) -> None:
        self._block("unexpected-download")
        await download.cancel()

    def _frame_navigated(self, frame) -> None:
        transition = self._transition
        if (frame != self.page.main_frame or transition is None or transition.seen != len(transition.requests)
                or not self._matches(transition.requests[-1], frame.url)):
            self._block("unexpected-browser-navigation")

    def _local_url(self, url: str) -> SplitResult | None:
        if "#" in url or "\\" in url or any(character.isspace() for character in url):
            return None
        try:
            target = urlsplit(url)
            if f"{target.scheme}://{target.netloc}" == self.profile.origin and _path(target.path):
                return target
        except ValueError:
            pass
        return None

    def _matches(self, expected: _Request, url: str) -> bool:
        target = self._local_url(url)
        if target is None or not _fields(target.query, expected.query):
            return False
        if expected.path is not None:
            return target.path == expected.path
        route = self.profile._draft_route(target.path)
        return route is not None and self._landing(route)

    async def _route(self, route: Route) -> None:
        # Also catches a popup's initial request when it is not attached to the
        # page's CDP session. Requests from a new frame can have no Playwright frame yet.
        request = route.request
        transition = self._transition
        try:
            bound_frame = request.frame == self.page.main_frame
        except Error:
            bound_frame = False
        if (self._violation is None and bound_frame and request.is_navigation_request() and transition is not None
                and any(entry.method == request.method and self._matches(entry, request.url)
                        for entry in transition.requests)):
            await route.continue_()
        else:
            self._block("unexpected-browser-request")
            await route.abort()

    async def _request_paused(self, event: dict) -> None:
        transition = self._transition
        request = event["request"]
        request_id = event["requestId"]
        if (self._violation is None and transition is not None and transition.seen < len(transition.requests)
                and event.get("frameId") == self._frame_id and event.get("resourceType") == "Document"):
            expected = transition.requests[transition.seen]
            if (expected.method == request["method"] and self._matches(expected, request["url"])
                    and (expected.form is None or (request.get("postData") is not None
                                                   and _fields(request["postData"], expected.form)))):
                transition.seen += 1
                await self._cdp.send("Fetch.continueRequest", {"requestId": request_id})
                return
        self._block("unexpected-browser-request")
        await self._cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})

    def _check_browser(self) -> None:
        if self._violation is not None:
            raise WebBlocked(self._violation)
        if self.page.is_closed() or self.page.context.pages != [self.page] or self.page.frames != [self.page.main_frame]:
            raise WebBlocked("browser-scope-lost")

    async def _navigate(self, requests: tuple[_Request, ...], activate: Callable[[], Awaitable[Response | None]]) -> None:
        self._check_browser()
        transition = _Transition(requests)
        self._transition = transition
        try:
            response = await activate()
            self._check_browser()
            if (response is None or not response.ok or transition.seen != len(requests)
                    or not self._matches(requests[-1], self.page.url)):
                self._block("native-transition-unconfirmed")
                raise WebBlocked("native-transition-unconfirmed")
        except Error:
            self._block("native-transition-unconfirmed")
            raise WebBlocked(self._violation) from None
        except asyncio.CancelledError:
            self._block("native-transition-interrupted")
            raise
        finally:
            self._transition = None

    async def _click_navigation(self, button: Locator) -> Response | None:
        async with self.page.expect_navigation(wait_until="load") as navigation:
            await button.click()
        return await navigation.value

    def _landing(self, route: _DraftRoute) -> bool:
        return route.section.route == self.profile.landing_section and route.row is None

    def _assign(self, identity: DraftIdentity) -> None:
        if identity.return_type not in self.profile.return_types:
            raise WebBlocked("unsupported-return-type")
        if self._identity is not None and self._identity != identity:
            raise WebBlocked("different-return-already-bound")
        self._identity = identity

    def _identity_fields(self, identity: DraftIdentity) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.profile.context.field_names,
                         (identity.client_reference, str(identity.tax_year), identity.return_type), strict=True))

    async def _verify_identity(self, root: Locator, identity: DraftIdentity) -> str:
        ctx = self.profile.context
        key, reference, year, kind, status = [await root.get_attribute(name) for name in ctx.identity_attributes]
        if not key or (reference, year, kind, status) != (
                identity.client_reference, str(identity.tax_year), identity.return_type, ctx.unfiled_value):
            raise WebBlocked("draft-identity-or-unfiled-status-mismatch")
        return key

    async def _list(self) -> Locator:
        self._check_browser()
        url = self._local_url(self.page.url)
        if url is None or url.path != self.profile.list_path:
            raise WebBlocked("return-list-not-active")
        root = await _one(self.page.get_by_role("main"))
        await _one(root.get_by_role("heading", name=self.profile.context.list_heading, exact=True))
        if await self.page.locator("base, dialog[open]").count():
            raise WebBlocked("unexpected-return-list-context")
        return root

    async def _link(self, link: Locator, scope: Locator) -> str:
        if link.page != self.page:
            raise WebBlocked("link-from-different-browser-page")
        link = await _one(link.and_(scope.get_by_role("link")))
        if (await link.get_attribute("target") is not None or await link.get_attribute("download") is not None
                or not (href := await link.get_attribute("href"))):
            raise WebBlocked("unexpected-link-action")
        target = self._local_url(urljoin(self.page.url, href))
        if target is None or target.query:
            raise WebBlocked("unexpected-link-destination")
        return target.path

    async def _form(self, form: Locator, button: Locator, *, path: str, method: str) -> None:
        if form.page != self.page or button.page != self.page:
            raise WebBlocked("form-from-different-browser-page")
        form = await _one(form.and_(self.page.locator("form")))
        button = await _one(button.and_(form.get_by_role("button")))
        owner = button.locator("xpath=ancestor::form[1]")
        if await form.and_(owner).count() != 1:
            raise WebBlocked("form-button-owner-mismatch")
        # Reject all submitter overrides including target/form associations. They
        # may be present when the form's own action still appears correct.
        for attribute in ("form", "formaction", "formmethod", "formtarget", "formenctype", "formnovalidate"):
            if await button.get_attribute(attribute) is not None:
                raise WebBlocked("unchecked-form-button-override")
        if (await button.get_attribute("type") not in (None, "submit")
                or not await button.is_enabled() or await form.get_attribute("target") is not None
                or await form.get_attribute("novalidate") is not None
                or await form.get_attribute("enctype") not in (None, "application/x-www-form-urlencoded")
                or (await form.get_attribute("method") or "get").upper() != method):
            raise WebBlocked("unexpected-native-form")
        action = await form.get_attribute("action")
        if not action or not self._matches(_Request(method, path), urljoin(self.page.url, action)):
            raise WebBlocked("unexpected-native-form-action")

    async def _creation_form(self, root: Locator, identity: DraftIdentity) -> tuple[Locator, Locator]:
        button = await _one(root.get_by_role("button", name=self.profile.context.create_button, exact=True))
        form = button.locator("xpath=ancestor::form[1]")
        await self._form(form, button, path=self.profile.create_path, method="POST")
        fields = self._identity_fields(identity)
        if (await form.locator("input[type=hidden]").count() != len(fields)
                or await form.locator("*").count() != len(fields) + 1):
            raise WebBlocked("unexpected-create-fields")
        for field_name, value in fields:
            hidden = form.locator(f'input[type="hidden"][name="{field_name}"]')
            if await hidden.count() != 1 or await hidden.input_value() != value:
                raise WebBlocked("create-identity-mismatch")
        return form, button

    async def _search(self, identity: DraftIdentity) -> tuple[SearchResult, Locator]:
        self._assign(identity)
        self._check_browser()
        current = self._local_url(self.page.url)
        if current is not None and current.path != self.profile.list_path:
            if self._key is None:
                raise WebBlocked("unbound-draft-context")
            await self._verified_draft(identity, self._key)
        elif current is not None:
            await self._list()
        elif self.page.url != "about:blank":
            raise WebBlocked("unexpected-browser-context")

        target = self.profile
        ctx = target.context
        await self._navigate((_Request("GET", target.list_path),),
                             lambda: self.page.goto(target.origin + target.list_path, wait_until="load"))
        root = await self._list()
        form = await _one(root.get_by_role("form", name=ctx.search_form, exact=True))
        button = await _one(form.get_by_role("button", name=ctx.search_button, exact=True))
        await self._form(form, button, path=target.list_path, method="GET")
        fields = self._identity_fields(identity)
        if (await form.locator("input,select,textarea").count() != len(fields)
                or await form.get_by_role("button", include_hidden=True).count() != 1):
            raise WebBlocked("unexpected-search-fields")
        try:
            for index, (label, (field_name, value)) in enumerate(zip(ctx.search_labels, fields, strict=True)):
                # An implicit label wrapping a native select also contains the
                # option text. Its combobox name excludes its own options.
                control = await _one(form.get_by_role("combobox", name=label, exact=True) if index == 2
                                     else form.get_by_label(label, exact=True))
                if await control.get_attribute("name") != field_name:
                    raise WebBlocked("search-field-identity-mismatch")
                if index == 2:
                    await control.select_option(value=value)
                else:
                    await control.fill(value)
                if await control.input_value() != value:
                    raise WebBlocked("search-value-rejected")
        except Error:
            raise WebBlocked("search-controls-rejected") from None
        await self._navigate((_Request("GET", target.list_path, fields),), lambda: self._click_navigation(button))
        root = await self._list()
        if await self.page.locator("body > *").count() != 1 or await root.and_(self.page.locator("body > main")).count() != 1:
            raise WebBlocked("incomplete-or-ambiguous-draft-search")
        coverage = await _one(root.locator(f"[{ctx.completion_attribute}]"))
        if (await coverage.get_attribute(ctx.completion_attribute) != ctx.completion_value
                or (await coverage.inner_text()).strip() != ctx.completion_text
                or await coverage.locator("*").count()
                or await self.page.locator('[role="alert"], [role="alertdialog"], [aria-live], nav, '
                                           '[role="navigation"], [aria-current="page"], [rel~="next"], [rel~="prev"]').count()):
            raise WebBlocked("incomplete-or-ambiguous-draft-search")
        heading = root.get_by_role("heading", name=ctx.list_heading, exact=True)
        form = await _one(root.get_by_role("form", name=ctx.search_form, exact=True))
        allowed = heading.or_(form).or_(coverage)
        results = root.get_by_role("region", name=ctx.result_region, exact=True, include_hidden=True)
        status = root.get_by_role("status", include_hidden=True)
        children = root.locator(":scope > *")
        links = root.get_by_role("link", include_hidden=True)
        buttons = root.get_by_role("button", include_hidden=True)
        count = await results.count()
        if count == 1 and await status.count() == 0:
            # Only the reviewed result, exact search and coverage marker may be
            # present. Unknown notices, pager controls and extra results fail closed.
            if (await children.count() != 4 or await children.and_(allowed.or_(results)).count() != 4
                    or await links.count() != 1 or await buttons.count() != 1 or await root.locator("form").count() != 1):
                raise WebBlocked("incomplete-or-ambiguous-draft-search")
            key = await self._verify_identity(await _one(results), identity)
            link = await _one(results.get_by_role("link", name=ctx.open_link, exact=True))
            path = await self._link(link, root)
            route = target._draft_route(path)
            if route is None or route.key != key or not self._landing(route):
                raise WebBlocked("search-result-link-mismatch")
            return SearchResult(identity, "found", key, uuid4().hex), root
        if count == 0 and await status.count() == 1 and await status.is_visible():
            if (await status.inner_text()).strip() == ctx.absent_status and not await status.locator("*").count():
                create_form, _ = await self._creation_form(root, identity)
                if (await children.count() == 5 and await children.and_(allowed.or_(status).or_(create_form)).count() == 5
                        and await links.count() == 0 and await buttons.count() == 2
                        and await root.locator("form").count() == 2):
                    return SearchResult(identity, "absent", None, uuid4().hex), root
        raise WebBlocked("incomplete-or-ambiguous-draft-search")

    async def search(self, identity: DraftIdentity) -> SearchResult:
        """Run a complete exact UI search; ambiguous results never prove absence."""
        async with self._lock:
            result, _ = await self._search(identity)
            return result

    async def open_existing(self, identity: DraftIdentity, key: str | None = None) -> str:
        async with self._lock:
            expected_key = self._key if key is None else key
            if self._key is not None and expected_key != self._key:
                raise WebBlocked("different-draft-already-bound")
            result, root = await self._search(identity)
            if result.draft_key is None:
                raise WebBlocked("draft-not-found")
            if expected_key is not None and result.draft_key != expected_key:
                raise WebBlocked("durable-draft-key-mismatch")
            ctx = self.profile.context
            link = root.get_by_role("region", name=ctx.result_region, exact=True).get_by_role(
                "link", name=ctx.open_link, exact=True,
            )
            path = await self._link(link, root)
            route = self.profile._draft_route(path)
            if route is None or route.key != result.draft_key or not self._landing(route):
                raise WebBlocked("search-result-link-mismatch")
            await self._navigate((_Request("GET", path),), lambda: self._click_navigation(link))
            await self._verified_draft(identity, result.draft_key)
            self._key = result.draft_key
            return self._key

    async def reopen(self, identity: DraftIdentity, key: str) -> str:
        if not key:
            raise WebBlocked("durable-draft-key-required")
        return await self.open_existing(identity, key)

    async def create(self, identity: DraftIdentity,
                     authorize: Callable[[SearchResult], Awaitable[CreatePermit]]) -> str:
        """Create once after the trusted journal commits intent for a proven absence."""
        async with self._lock:
            if self._key is not None or self._create_intent is not None:
                raise WebBlocked("create-already-dispatched-or-draft-bound")
            absent, _ = await self._search(identity)
            if absent.status != "absent":
                raise WebBlocked("draft-already-exists")
            permit = await authorize(absent)
            if (not isinstance(permit, CreatePermit) or permit.profile_id != self.profile.profile_id
                    or permit.identity != identity or permit.proof != absent.proof
                    or not isinstance(permit.intent_id, str) or not permit.intent_id.strip()):
                raise WebBlocked("creation-intent-not-authorized")
            self._create_intent = permit.intent_id
            # Refresh against the native list after journaling, not the previous
            # DOM snapshot. Production still requires exclusive external checkout.
            confirmed, root = await self._search(identity)
            if confirmed.status != "absent":
                raise WebBlocked("draft-no-longer-absent")
            _, button = await self._creation_form(root, identity)
            target = self.profile
            await self._navigate((_Request("POST", target.create_path, form=self._identity_fields(identity)),
                                  _Request("GET")), lambda: self._click_navigation(button))
            url = self._local_url(self.page.url)
            route = target._draft_route(url.path) if url is not None else None
            if route is None or not self._landing(route):
                raise WebBlocked("create-result-unconfirmed")
            await self._verified_draft(identity, route.key)
            self._key = route.key
            return self._key

    async def _verified_draft(self, identity: DraftIdentity, key: str) -> tuple[Locator, _DraftRoute]:
        self._check_browser()
        self._assign(identity)
        url = self._local_url(self.page.url)
        route = self.profile._draft_route(url.path) if url is not None and not url.query else None
        if route is None or key != route.key or (self._key is not None and self._key != key):
            raise WebBlocked("draft-route-or-key-mismatch")
        ctx = self.profile.context
        root = await _one(self.page.get_by_role("main").and_(self.page.locator(ctx.draft_root)))
        if (await self.page.locator("base").count() or await self._verify_identity(root, identity) != key
                or await root.get_attribute(ctx.section_attribute) != route.section.context):
            raise WebBlocked("draft-context-mismatch")
        dialogs = self.page.get_by_role("dialog")
        if route.section.dialog:
            await _one(dialogs.and_(root.get_by_role("dialog")))
            if await dialogs.count() != 1:
                raise WebBlocked("unexpected-dialog-context")
        elif await dialogs.count():
            raise WebBlocked("unexpected-dialog-context")
        if route.row is not None:
            scope = dialogs if route.section.dialog else root
            button = await _one(scope.get_by_role("button", name=route.section.save_button, exact=True))
            form = button.locator("xpath=ancestor::form[1]")
            await self._form(form, button, path=url.path, method="POST")
            expected_row = None if route.row == "new" else route.row
            if await form.get_attribute(ctx.row_attribute) != expected_row:
                raise WebBlocked("draft-record-key-mismatch")
        return root, route

    async def require_draft(self, identity: DraftIdentity, key: str) -> tuple[str, str | None]:
        async with self._lock:
            if self._key is None:
                raise WebBlocked("draft-not-bound")
            _, route = await self._verified_draft(identity, key)
            return route.section.context, route.row

    async def follow(self, identity: DraftIdentity, key: str, link: Locator, *, path: str) -> None:
        """Follow a vetted same-draft UI link to an exact declared path."""
        async with self._lock:
            if self._key is None:
                raise WebBlocked("draft-not-bound")
            root, _ = await self._verified_draft(identity, key)
            destination = self.profile._draft_route(path)
            if destination is None or destination.key != key or await self._link(link, root) != path:
                raise WebBlocked("draft-link-outside-requested-scope")
            await self._navigate((_Request("GET", path),), lambda: self._click_navigation(link))
            await self._verified_draft(identity, key)

    async def submit(self, identity: DraftIdentity, key: str, form: Locator, button: Locator, *,
                     path: str, result_path: str, expected_fields: tuple[tuple[str, str], ...]) -> None:
        """Dispatch only the profile's Save button for this exact native draft form."""
        async with self._lock:
            if self._key is None:
                raise WebBlocked("draft-not-bound")
            root, route = await self._verified_draft(identity, key)
            if not self._matches(_Request("POST", path), self.page.url) or (route.section.rows and route.row is None):
                raise WebBlocked("save-form-outside-requested-scope")
            parent = path.rsplit("/", 1)[0] if route.row is not None else path
            if result_path != parent or self.profile._draft_route(result_path) != _DraftRoute(key, route.section, None):
                raise WebBlocked("save-result-outside-requested-scope")
            scope = root.get_by_role("dialog") if route.section.dialog else root
            button = await _one(button.and_(scope.get_by_role("button", name=route.section.save_button, exact=True)))
            await self._form(form, button, path=path, method="POST")
            if not expected_fields or len({name for name, _ in expected_fields}) != len(expected_fields):
                raise WebBlocked("unverified-save-fields")
            await self._navigate((_Request("POST", path, form=expected_fields), _Request("GET", result_path)),
                                 lambda: self._click_navigation(button))
            await self._verified_draft(identity, key)
