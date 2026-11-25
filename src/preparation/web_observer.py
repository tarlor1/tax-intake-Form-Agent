"""Read-only native focus observation for a dedicated Chromium reviewer page."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from uuid import uuid4

from playwright.async_api import CDPSession, Error, Page

from .native_plan import DraftIdentity
from .sigma_web import WebBlocked, WebProfile

Selection = tuple[str, str, str | None, str | None]
SelectionCallback = Callable[[Selection | None], None]


@dataclass(frozen=True, slots=True)
class ReviewSection:
    section_key: str
    fields: tuple[tuple[str, str], ...]  # (profile-approved CSS control selector, semantic key)
    active_record: str | None = None  # selector for the current row when the URL/control does not carry it
    record_attributes: tuple[str, ...] = ()  # attributes affecting the active-record selector


_SCRIPT = r"""
(config => {
    if (globalThis[config.state] || window !== window.top || location.origin !== config.origin) return;
    const cleanups = [];
    const listen = (target, event, fn) => {
        target.addEventListener(event, fn, {capture: true, passive: true});
        cleanups.push(() => target.removeEventListener(event, fn, true));
    };
    let hidden = false;
    let lastPayload;
    const report = (focus = document.activeElement) => {
        let selection = null;
        try {
            if (!hidden && window.length === 0 && location.origin === config.origin &&
                !location.search && !location.hash) {
                const roots = document.querySelectorAll(config.root);
                const root = roots.length === 1 ? roots[0] : null;
                const section = root?.getAttribute(config.sectionAttr);
                if (root?.matches('main:not([role]),[role="main"]') && root.getClientRects().length &&
                    !document.querySelector('base') && Object.hasOwn(config.sections, section) && config.identity.every(
                    ([attribute, value]) => root.getAttribute(attribute) === value
                )) {
                    const {fields = [], active_record, dialog} = config.sections[section];
                    const dialogs = [...document.querySelectorAll('dialog[open],[role="dialog"]')]
                        .filter(element => element.getClientRects().length);
                    if (dialog ? dialogs.length !== 1 || !root.contains(dialogs[0]) : dialogs.length !== 0)
                        throw new Error('Unsupported modal context');
                    const scope = dialog ? dialogs[0] : root;
                    const inside = focus instanceof Element && scope.contains(focus);
                    const row = inside && focus.closest(`[${config.rowAttr}]`);
                    const active = active_record ? root.querySelectorAll(active_record) : [];
                    const scopedRow = row && root.contains(row) ? row : null;
                    const rowKey = scopedRow?.getAttribute(config.rowAttr) ?? null;
                    const activeKey = active.length === 1 ? active[0].getAttribute(config.rowAttr) : null;
                    const ambiguous = active.length > 1 || (active.length === 1 && !activeKey) ||
                        (rowKey && activeKey && rowKey !== activeKey);
                    const matches = !ambiguous && inside && !focus.matches(':disabled') &&
                        focus.getClientRects().length ? fields.filter(([selector]) =>
                            focus.matches(selector) && (scopedRow ?? root).querySelectorAll(selector).length === 1) : [];
                    selection = [location.pathname, section,
                                 ambiguous ? null : matches.length === 1 ? rowKey ?? activeKey : activeKey,
                                 matches.length === 1 ? matches[0][1] : null];
                }
            }
        } catch (_) { /* An unsupported DOM state must clear evidence. */ }
        const payload = JSON.stringify(selection);
        if (payload !== lastPayload) {
            lastPayload = payload;
            globalThis[config.binding](payload);
        }
    };
    listen(document, 'focusin', event => { if (event.isTrusted) report(event.target); });
    listen(window, 'popstate', () => report());
    listen(window, 'pageshow', () => { hidden = false; report(); });
    listen(window, 'pagehide', () => { hidden = true; report(); });
    const observer = new MutationObserver(() => report());
    observer.observe(document, {childList: true, subtree: true, attributes: true,
                                attributeFilter: config.attributes});
    globalThis[config.state] = {
        report,
        stop: () => { observer.disconnect(); for (const cleanup of cleanups) cleanup(); }
    };
    report();
})
"""


class WebSelectionObserver:
    """Attach only to a caller-owned human review page with an authorized draft grant.

    The callback receives only the verified native selection or None (detached).
    A null field denotes manual selection. CDP binding messages are private to the
    observer; they never dispatch app actions or travel through a page HTTP request.
    """

    def __init__(
        self, page: Page, profile: WebProfile, identity: DraftIdentity, draft_key: str,
        sections: tuple[ReviewSection, ...], record_key_pattern: str, on_selection: SelectionCallback,
    ) -> None:
        self._page = page
        self._profile = profile
        self._key = draft_key
        self._notify = on_selection
        self._record_key = re.compile(record_key_pattern, re.ASCII)
        self._fields = {section.section_key: {key for _, key in section.fields} for section in sections}
        context = profile.context
        if (len(self._fields) != len(sections)
                or not self._fields.keys() <= {section.context for section in profile.sections}
                or identity.return_type not in profile.return_types
                or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", attr) for attr in (
                    *context.identity_attributes, context.section_attribute, context.row_attribute,
                    *(attr for section in sections for attr in section.record_attributes),
                ))):
            raise ValueError("Invalid native review observation profile.")
        self._world = "review_" + uuid4().hex
        self._binding = self._world + "_notify"
        self._config = {
            "state": self._world, "binding": self._binding, "origin": profile.origin,
            "root": context.draft_root, "sectionAttr": context.section_attribute,
            "rowAttr": context.row_attribute,
            "identity": list(zip(context.identity_attributes, (
                draft_key, identity.client_reference, str(identity.tax_year),
                identity.return_type, context.unfiled_value,
            ), strict=True)),
            "sections": {section.context: {"dialog": section.dialog} for section in profile.sections},
            "attributes": sorted({
                *context.identity_attributes, context.section_attribute, context.row_attribute,
                "disabled", "hidden", "inert", "open", "role",
                *(attr for section in sections for attr in section.record_attributes),
            }),
        }
        for section in sections:
            self._config["sections"][section.section_key].update(
                fields=section.fields, active_record=section.active_record,
            )
        self._cdp: CDPSession | None = None
        self._frame = ""
        self._script_id: str | None = None
        self._contexts: set[int] = set()
        self._source: int | None = None
        self._last: Selection | None = None
        self._emitted = False
        self._closed = False
        self._refreshes: set[asyncio.Task] = set()

    async def __aenter__(self) -> WebSelectionObserver:
        if self._closed or self._cdp is not None:
            raise RuntimeError("A review observer can attach only once.")
        try:
            cdp = self._cdp = await self._page.context.new_cdp_session(self._page)
            await cdp.send("Page.enable")
            self._frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            cdp.on("Runtime.executionContextCreated", self._created)
            cdp.on("Runtime.executionContextDestroyed", lambda event: self._destroyed(event["executionContextId"]))
            cdp.on("Runtime.executionContextsCleared", self._cleared)
            cdp.on("Runtime.bindingCalled", self._called)
            cdp.on("Page.navigatedWithinDocument", self._within_document)
            cdp.on("Page.frameAttached", self._refresh)
            cdp.on("Page.frameDetached", self._refresh)
            self._page.on("close", self._page_closed)
            self._page.on("crash", self._page_closed)
            await cdp.send("Runtime.enable")
            await cdp.send("Runtime.addBinding", {"name": self._binding, "executionContextName": self._world})
            script = await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": f"{_SCRIPT}({json.dumps(self._config)})", "worldName": self._world,
                "runImmediately": True,
            })
            self._script_id = script["identifier"]
            if not self._emitted:
                self._emit(None)
            return self
        except Error:
            await self.close()
            raise WebBlocked("unable-to-attach-native-review-observer") from None
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    def _emit(self, selection: Selection | None) -> None:
        if not self._emitted or selection != self._last:
            self._emitted, self._last = True, selection
            self._notify(selection)

    def _created(self, event: dict) -> None:
        context = event["context"]
        aux = context.get("auxData", {})
        if (not self._closed and context["name"] == self._world and aux.get("frameId") == self._frame
                and aux.get("type") == "isolated" and not aux.get("isDefault")):
            self._contexts.add(context["id"])

    def _destroyed(self, context: int) -> None:
        self._contexts.discard(context)
        if self._source == context:
            self._source = None
            self._emit(None)

    def _cleared(self, _event: dict) -> None:
        self._contexts.clear()
        self._source = None
        self._emit(None)

    def _called(self, event: dict) -> None:
        context = event["executionContextId"]
        if self._closed or event["name"] != self._binding or context not in self._contexts:
            return
        payload = event["payload"]
        self._source = context
        selection = None
        try:
            if len(payload) <= 2048:
                selection = self._validate(json.loads(payload))
        except (TypeError, ValueError):
            pass
        self._emit(selection)

    def _validate(self, data: object) -> Selection | None:
        if not isinstance(data, list) or len(data) != 4:
            return None
        path, section, row, field = data
        if not isinstance(path, str) or not isinstance(section, str):
            return None
        route = self._profile._draft_route(path)
        if route is None or route.key != self._key or route.section.context != section:
            return None
        if (not route.section.rows or not isinstance(row, str) or len(row) > 128
                or not self._record_key.fullmatch(row) or (route.row is not None and route.row != row)):
            row = None
        if ((route.section.rows and row is None) or not isinstance(field, str)
                or field not in self._fields.get(section, ())):
            field = None
        return (self._key, section, row, field)

    def _within_document(self, event: dict) -> None:
        if event["frameId"] == self._frame:
            self._refresh(event)

    def _refresh(self, _event: dict) -> None:
        if not self._closed and self._source is not None:
            task = asyncio.create_task(self._report_current(self._source))
            self._refreshes.add(task)
            task.add_done_callback(self._refreshes.discard)

    async def _report_current(self, context: int) -> None:
        if self._cdp is not None and not self._closed and context in self._contexts:
            with suppress(Error):
                await self._cdp.send("Runtime.evaluate", {
                    "expression": f"globalThis.{self._world}?.report()", "contextId": context,
                })

    def _page_closed(self, *_args) -> None:
        self._closed = True
        self._emit(None)

    async def close(self) -> None:
        self._page_closed()
        self._page.remove_listener("close", self._page_closed)
        self._page.remove_listener("crash", self._page_closed)
        for task in self._refreshes:
            task.cancel()
        if self._refreshes:
            await asyncio.gather(*self._refreshes, return_exceptions=True)
        cdp, self._cdp = self._cdp, None
        if cdp is None:
            return
        if self._script_id is not None:
            with suppress(Error):
                await cdp.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self._script_id})
        for context in tuple(self._contexts):
            with suppress(Error):
                await cdp.send("Runtime.evaluate", {
                    "expression": f"globalThis.{self._world}?.stop()", "contextId": context,
                })
        with suppress(Error):
            await cdp.send("Runtime.removeBinding", {"name": self._binding})
        with suppress(Error):
            await cdp.detach()
        self._contexts.clear()
        self._source = None
