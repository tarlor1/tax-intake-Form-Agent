"""Run the interactive synthetic tax preparation and evidence review demo."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from collections.abc import Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import os
from pathlib import Path
import re
from shutil import which
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

from dotenv import dotenv_values
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from playwright.async_api import Error as PlaywrightError

from src.config import DEFAULT_CLAUDE_MODEL, DEFAULT_GEMINI_MODEL, PROJECT_ROOT
from src.preparation.agent import Authorization, PreparationBlocked, StagePolicy
from src.preparation.batches import BatchFile, BatchManifest, BatchView, ClassifiedUpload, IngestScope
from src.preparation.coordinator_db import CoordinatorDB, migrate
from src.preparation.dispatch import UploadCommitted
from src.preparation.native_plan import DraftIdentity
from src.preparation.originals import FilesystemOriginals
from src.preparation.providers import Provider, ProviderError, ProviderIdentity, ProviderTurn, ToolCall, Usage, open_provider
from src.preparation.section_planner import plan_individual
from src.preparation.sigma_web import WebDraft
from src.preparation.site import ExtractionEnrollment, FirmRuntime, FirmSite
from src.preparation.tax_store import TaxAccess
from src.preparation.web_actions import WebLayout
from src.preparation.web_worker import WebWorker
from src.preparation.worker import WorkerEnrollment, WorkerPlan

from src.local_postgres import local_postgres
from .discovery import FormDrivenExtraction, FormProposal
from .documents import SUBJECT_NAME, SUBJECT_REFERENCE, TAX_YEAR, extract_numbered, recognized_documents, verify_document
from .native import PracticeAuthority, layout, practice_web_profile, rows
from .review import PairedCheck, PracticePreparer, UploadCheck, paired_review, practice_companion
from .sources import individual_sources
from .target import sigma_practice_target

Case = Literal["wages", "mixed", "rideshare"]
_CASES = ("wages", "mixed", "rideshare")
_PERSON = DraftIdentity(client_reference=SUBJECT_REFERENCE, tax_year=TAX_YEAR, return_type="individual")
_CLASSES = frozenset({"synthetic"})
_LOCAL = ProviderIdentity("gemini", "local-deterministic", "https://network-denied.invalid", "practice-local")
_USAGE = Usage(0, 0)  # Offline adapters have no SDK client, credential, or network transport.
_LIVE_PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", DEFAULT_GEMINI_MODEL, "https://generativelanguage.googleapis.com"),
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", None, "https://api.openai.com/v1"),
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL, "https://api.anthropic.com"),
}
_RETURN, _STORE, _SEAT, _WORKER = "practice-return", "practice-store", "practice-seat", "practice-worker"
_CLIENT = "practice-client"
_LABELS = {"w2": "W-2", "rideshare": "Rideshare Schedule C", "expenses": "Expenses", "documents": "Documents"}
_NEC = "unresolved-1099-nec-overlap"


class PracticeError(RuntimeError):
    """The offline run did not establish the expected outcome."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise PracticeError(message)


def _say(message: str) -> None:
    print(message, flush=True)


def _policy(stage: Literal["extraction", "reasoning"]) -> StagePolicy:
    return StagePolicy(_LOCAL, stage, _CLASSES, 48, 65_536, 120, 8192, 2048,
                       Decimal(0), Decimal(0), Decimal(0))


def _live_settings(provider_name: str | None) -> tuple[StagePolicy, str]:
    # Read the project's existing configuration only for an explicit live run.
    # Do not mutate the process environment or send anything to a second provider.
    configured = dotenv_values(PROJECT_ROOT / ".env", interpolate=False)

    def value(name: str) -> str:
        return (os.environ.get(name, configured.get(name)) or "").strip()

    provider = provider_name or value("SIGMA_PRACTICE_PROVIDER")
    if not provider:
        available = [name for name, (key, *_rest) in _LIVE_PROVIDERS.items() if value(key)]
        if len(available) != 1:
            raise PracticeError("Configure one provider key or select --provider gemini, openai, or anthropic.")
        provider, = available
    if provider not in _LIVE_PROVIDERS:
        raise PracticeError("Select gemini, openai, or anthropic for live reasoning.")
    key_name, model_name, default_model, endpoint = _LIVE_PROVIDERS[provider]
    key = value(key_name)
    if not key or any(not "!" <= character <= "~" for character in key):
        raise PracticeError(f"Live {provider} reasoning requires a valid {key_name}.")
    model = value(f"SIGMA_PRACTICE_{provider.upper()}_MODEL") or value(model_name) or default_model
    if not model or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", model):
        raise PracticeError(f"Set {model_name} to an approved tool-calling model ID.")
    identity = ProviderIdentity(provider, model, endpoint, f"practice-{provider}")
    policy = StagePolicy(identity, "reasoning", _CLASSES, 48, 65_536, 300, 16_384, 2048,
                         Decimal("10"), Decimal("40"), Decimal("12"))
    return policy, key


class _FixtureExtraction:
    identity = _LOCAL

    async def extract(self, instructions, source_text, schema, *, max_output_tokens, max_request_bytes, before_request):
        await before_request()
        extracted = extract_numbered(source_text)
        if schema.get("title") == FormProposal.__name__:
            forms = {record.identity.form for record in extracted.records}
            form = next(iter(forms)) if len(forms) == 1 else "unknown"
            return FormProposal.model_validate({"form": form}).model_dump(), _USAGE
        return extracted.model_dump(mode="json"), _USAGE


@dataclass(frozen=True)
class _Step:
    context: str
    name: str
    field: str


class _NextPermittedAction:
    """Walk the allowed choices toward unfinished assignments, not a prepared action script."""

    identity = _LOCAL

    def __init__(self, profile: WebLayout, plan: WorkerPlan, steps: list[_Step], delay: float):
        self._links = {section.key: tuple(link.destination for link in section.links) for section in profile.sections}
        self._saves = {section.key: section.save for section in profile.sections}
        self._remaining = {section.section_key for section in plan.draft.sections}
        self._opened: set[str] = set()
        self._steps, self._delay = steps, delay

    def _distance(self, start: str, origin: str) -> int | None:
        todo = deque([(start, 0)])
        seen = {origin, start}
        while todo:
            context, distance = todo.popleft()
            if context in self._remaining:
                return distance
            for target in self._links[context]:
                if target not in seen:
                    seen.add(target)
                    todo.append((target, distance + 1))
        return None

    def _choose(self, context: str, choices: list[dict]) -> dict:
        # Fill all currently allowed fields before saving; a pending form has no navigation choices.
        for action in ("set_fields", "set_field", "save_draft", "ensure_record"):
            for choice in choices:
                if choice["name"] != action:
                    continue
                if action == "ensure_record" and choice["arguments"]["sourceRecordId"] in self._opened:
                    continue
                return choice
        # A matched existing row may have nothing editable. Return to its same-section list.
        for choice in choices:
            if choice["name"] == "open_detail" and choice["field"] == context:
                return choice

        self._remaining.discard(context)
        routes = []
        for choice in choices:
            if choice["name"] in {"open_section", "open_detail"}:
                distance = self._distance(choice["field"], context)
                if distance is not None:
                    routes.append((distance, choice))
        if routes:
            return min(routes, key=lambda route: route[0])[1]
        _say(f"Needs review: no permitted action can finish the assignments from {_LABELS.get(context, context)}.")
        raise PreparationBlocked("practice-no-safe-next-action")

    async def propose(self, observation, tools, previous, *, max_output_tokens, max_request_bytes):
        context = observation["context"]
        allowed = {tool.name for tool in tools}
        choices = [choice for choice in observation["choices"] if choice["name"] in allowed]
        choice = self._choose(context, choices)
        name, field, args = choice["name"], choice["field"], choice["arguments"]
        if name == "ensure_record":
            self._opened.add(args["sourceRecordId"])
            action = "Match or add next assigned record"
        elif name == "set_field":
            action = f"Enter verified {field}"
        elif name == "set_fields":
            action = f"Enter {len(args['fields'])} verified fields"
        elif name == "save_draft":
            action = self._saves[context]
        elif field == context:
            action = "Return to record list"
        else:
            action = f"Open {_LABELS.get(field, field)}"
        self._steps.append(_Step(context, name, field))
        _say(f"  {len(self._steps):2}. {_LABELS.get(context, context)}: {action}")
        if self._delay:
            await asyncio.sleep(self._delay)
        return ProviderTurn((ToolCall(f"practice-choice-{len(self._steps)}", name, args),), _USAGE)


class _LiveNavigation:
    """Supply progress context to the model without selecting or executing any action."""

    def __init__(self, provider: Provider, plan: WorkerPlan):
        self._provider = provider
        self._sections = {section.section_key: [record.source_record_id for record in section.records]
                          for section in plan.draft.sections}
        self._history: list[dict[str, str]] = []
        self._last: dict[str, str] | None = None

    @property
    def identity(self) -> ProviderIdentity:
        return self._provider.identity

    async def propose(self, observation, tools, previous, *, max_output_tokens, max_request_bytes):
        if previous is not None and self._last is not None:
            entry = {**self._last, "result": previous.result["status"]}
            self._history.append(entry)
            _say(f"  {len(self._history):2}. {entry['context']}: {entry['action']} {entry['field']} → {entry['result']}")
        context = observation["context"]
        try:
            turn = await self._provider.propose(
                {**observation, "assignedSectionsAndRecords": self._sections, "priorActions": self._history,
                 "workflow": (
                     "Prepare all assigned sections. Copy one current choice's name and arguments exactly. "
                     "On a record list, match or add a source record not already saved. Fill all offered fields "
                     "before saving. A successful row save returns to its list. Use prior action results to "
                     "avoid repeating saved work. Navigate to unfinished sections, using intermediate sections "
                     "only for transit."
                 )},
                tools, previous, max_output_tokens=max_output_tokens, max_request_bytes=max_request_bytes,
            )
        except ProviderError as error:
            _say(f"{self.identity.provider} reasoning stopped: {error}")
            raise
        self._last = None
        if len(turn.calls) == 1:
            call = turn.calls[0]
            for choice in observation["choices"]:
                if (call.name, call.arguments) == (choice["name"], choice["arguments"]):
                    if call.name == "finish_draft":
                        _say(f"  {self.identity.provider} requested completion; verifying the reopened draft.")
                    self._last = {"context": context, "action": call.name, "field": choice["field"]}
                    if call.name == "ensure_record":
                        self._last["record"] = choice["arguments"]["sourceRecordId"]
                    break
        return turn


class _Attestations:
    """Bind practice-only proofs to the worker's checked loopback native observations."""

    def __init__(self, authority: PracticeAuthority):
        self._authority = authority

    async def checkout(self, grant, item, draft):
        self._authority.checkouts.add((grant, draft, "practice-checkout"))
        return "practice-checkout"

    async def absent(self, grant, result):
        _require(result.status == "absent" and result.identity == _PERSON and result.draft_key is None,
                 "Synthetic absence did not match the enrolled draft identity.")
        self._authority.absent.add((grant, result.proof))
        _say("Open Return list: exact search found no draft. Journal Create Draft before dispatch.")
        return result.proof

    async def existing(self, grant, result):
        _require(result.status == "found" and result.identity == _PERSON and result.draft_key,
                 "Synthetic lookup did not prove the exact existing draft.")
        self._authority.existing[(grant, result.proof)] = result.draft_key
        return result.proof

    async def created(self, grant, step_id, draft):
        self._authority.created[(grant, step_id, "practice-created")] = draft
        self._authority.checkouts.add((grant, draft, "practice-checkout"))
        _say("Open the draft in the private MCP browser; the application's landing page is its W-2 list.")
        return "practice-created"

    async def closed(self, grant, draft):
        self._authority.checkouts = {entry for entry in self._authority.checkouts if entry[0] != grant}
        self._authority.closes.add((grant, draft, "practice-closed"))
        return "practice-closed"


@dataclass(frozen=True)
class _SavedDraft:
    wages: frozenset[tuple[str, str, Decimal | None, Decimal | None]]
    gross: Decimal | None
    fees: Decimal | None
    expenses: frozenset[tuple[str, str, Decimal]]
    box_3: frozenset[tuple[str, str, Decimal]]


@dataclass(frozen=True)
class PracticeResult:
    state: str
    decisions: tuple[tuple[str, str], ...]
    issues: tuple[str, ...]
    saved: _SavedDraft


async def _follow_saved(browser: WebDraft, key: str, label: str, path: str) -> None:
    await browser.follow(_PERSON, key, browser.page.get_by_role("link", name=label, exact=True),
                         path=f"/drafts/{key}/{path}")


async def _read_saved(profile, chromium: str | None, key: str) -> _SavedDraft:
    # Fresh browser and exact return-list lookup; nothing carried over from the worker window.
    async with WebDraft.launch(profile, executable_path=chromium) as browser:
        await browser.open_existing(_PERSON, key)
        wage_rows = await rows(browser, "W-2 records")
        wages = frozenset(
            (employer, instance, Decimal(wage) if wage else None, Decimal(withheld) if withheld else None)
            for employer, instance, wage, withheld, *_ in wage_rows
        )
        box_3 = frozenset((employer, instance, Decimal(box))
                          for employer, instance, _, _, *optional in wage_rows for box in optional)
        await _follow_saved(browser, key, "Rideshare", "rideshare")
        gross = await browser.page.get_by_label("Gross receipts", exact=True).input_value()
        fees = await browser.page.get_by_label("Platform fees", exact=True).input_value()
        await _follow_saved(browser, key, "Expenses", "rideshare/expenses")
        expenses = frozenset((reference, description, Decimal(value))
                             for reference, description, value in await rows(browser, "Expense records"))
    return _SavedDraft(wages, Decimal(gross) if gross else None, Decimal(fees) if fees else None, expenses, box_3)


def _display(saved: _SavedDraft) -> None:
    _say("Independently reopened saved draft from the Return list (read-only):")
    if not saved.wages:
        _say("  W-2: no saved rows")
    for employer, instance, wages, withholding in sorted(saved.wages, key=lambda row: row[:2]):
        wage_text = f"${wages:,.2f}" if wages is not None else "blank"
        withholding_text = f"${withholding:,.2f}" if withholding is not None else "blank"
        _say(f"  W-2 {employer} / {instance}: wages {wage_text}, withholding {withholding_text}")
    for employer, instance, value in sorted(saved.box_3):
        _say(f"  W-2 {employer} / {instance}: Box 3 Social Security wages ${value:,.2f}")
    if saved.gross is None and saved.fees is None:
        _say("  Rideshare Schedule C: gross receipts and fees blank")
    else:
        _say(f"  Rideshare Schedule C: gross receipts {saved.gross}, platform fees {saved.fees}")
    if not saved.expenses:
        _say("  Expenses: no saved rows")
    for reference, description, value in sorted(saved.expenses):
        _say(f"  Expense {reference}: {description}, ${value:,.2f}")


def _check_run(case: Case, result: PracticeResult, steps: list[_Step], mutations, view) -> None:
    decisions = result.decisions
    should_have_wages = case != "rideshare"
    should_have_rideshare = case != "wages"
    selected = {"w2"} if should_have_wages else set()
    if should_have_rideshare:
        selected.update({"rideshare", "expenses"})
    statuses = {(section, status) for section, status in decisions if section in {"w2", "rideshare", "expenses"}}
    _require(statuses == {(section, "selected" if section in selected else "skipped")
                          for section in {"w2", "rideshare", "expenses"}}, f"Unexpected section decisions: {decisions}")
    _require(("documents", "needs_review") in decisions if case == "mixed" else
             all(status != "needs_review" for _, status in decisions), f"Unexpected manual review: {decisions}")
    _require(result.state == ("needs_attention" if case == "mixed" else "ready_for_review"),
             f"Unexpected job status: {result.state}")
    _require(result.issues == ((_NEC,) if case == "mixed" else ()), f"Unexpected journal issues: {result.issues}")
    expected_wages = frozenset({("TEST-EMP-A", "A-1", Decimal(30000), Decimal(1600)),
                                ("TEST-EMP-B", "B-1", Decimal(30000), Decimal(800))}) if should_have_wages else frozenset()
    expected_expenses = (frozenset((receipt, "North Auto: Supplies", Decimal(45)) for receipt in ("ROTOR-102", "ROTOR-841"))
                         if should_have_rideshare else frozenset())
    _require(result.saved == _SavedDraft(expected_wages, Decimal(6200) if should_have_rideshare else None,
                                        Decimal(600) if should_have_rideshare else None, expected_expenses, frozenset()),
             f"Unexpected native values or row multiplicity after reopen: {result.saved}")
    _require({step.context for step in steps if step.name == "save_draft"} == selected,
             "A selected section was not saved or a skipped section received a save attempt.")
    destinations = {step.field for step in steps if step.name in {"open_section", "open_detail"}}
    _require(destinations <= selected, "The worker navigated into an unselected section.")
    _require(all(step["state"] == "readback" for step in mutations)
             and sum(step["kind"] == "save_draft" for step in mutations) ==
             len(expected_wages) + len(expected_expenses) + int(should_have_rideshare),
             "Native mutation journal did not acknowledge every expected save.")
    expected_fields = len(expected_wages) * 2 + len(expected_expenses) * 2 + 2 * int(should_have_rideshare)
    _require(len(view.fields) == expected_fields and all(field.state.reopened is not None for field in view.fields.values()),
             "Not all expected fields have durable reopen evidence.")
    for field in view.fields.values():
        facts = [view.facts[fact_id] for fact_id in field.state.used_candidate_ids]
        _require(facts and all(fact.eligible and all(c.original == fact.original for c in fact.citations) for fact in facts),
                 "A reopened value lacks evidence from its exact original version.")
        if field.binding.field_key == "gross":
            corroborating = [fact for fact in view.facts.values()
                             if fact.fact_key == "gross-receipts" and fact.eligible
                             and fact.normalized == facts[0].normalized]
            _require({view.records[fact.record_id].identity.form for fact in corroborating} == {"1099k", "platform_summary"}
                     and len({fact.original for fact in corroborating}) == 2,
                     "Gross receipts did not preserve both independently verified originals.")
            _say("  Gross receipts entered once; the 1099-K and matching platform summary remain in evidence.")
    for field_key in ("wages", "expense"):
        matching = [field for field in view.fields.values() if field.binding.field_key == field_key]
        if matching:
            _require(len({field.binding.record_key for field in matching}) == 2
                     and len({fact_id for field in matching for fact_id in field.state.used_candidate_ids}) == 2,
                     f"Equal-valued {field_key} entries did not preserve two record identities and facts.")
    precision = _show_precision(view)
    _require(not precision["document"], "A practice source was identified without a verified page or line.")


def _show_precision(view) -> Counter[str]:
    precision = Counter(c.precision for fact in view.facts.values() for c in fact.citations)
    _say("Grounded citation precision: " + ", ".join(f"{kind}={precision[kind]}" for kind in ("region", "lines", "page", "document")))
    return precision


async def _pause() -> None:
    try:
        await asyncio.to_thread(input, "Press Enter to stop the temporary practice target. ")
    except EOFError:
        pass


async def run_case(db: CoordinatorDB, directory: Path, case: Case, *, headless: bool,
                   wait: bool = False, chromium: str | None = None, check: PairedCheck | None = None) -> PracticeResult:
    """Run one generated batch against a private target and a disposable coordinator."""
    if case not in _CASES:
        raise ValueError(f"Unknown practice case: {case}")
    if check is not None and (headless or not wait):
        raise ValueError("A paired browser check requires visible review with wait=True.")
    return await _run_practice(db, directory, case, headless=headless, wait=wait, chromium=chromium, check=check)


async def run_interactive(
    db: CoordinatorDB, directory: Path, *, chromium: str | None = None, upload: UploadCheck | None = None,
    check: PairedCheck | None = None, live: tuple[StagePolicy, str] | None = None,
) -> PracticeResult:
    """Prepare one UI upload; optional browser callbacks drive upload and paired review in tests."""
    return await _run_practice(db, directory, None, headless=False, wait=True, chromium=chromium,
                               check=check, upload=upload, live=live)


async def _run_practice(
    db: CoordinatorDB, directory: Path, case: Case | None, *, headless: bool, wait: bool,
    chromium: str | None, check: PairedCheck | None, upload: UploadCheck | None = None,
    live: tuple[StagePolicy, str] | None = None,
) -> PracticeResult:
    reasoning, api_key = live if live is not None else (_policy("reasoning"), None)
    chromium = chromium or next((binary for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome")
                                 if (binary := which(name))), None)
    with TemporaryDirectory(prefix="run-", dir=directory) as temporary:
        work = Path(temporary).resolve()
        with sigma_practice_target(work / "drafts.sqlite3", include_box3=case is None) as origin:
            async with AsyncExitStack() as stack:
                return await _run(db, work, origin, case, headless=headless, chromium=chromium, wait=wait, check=check,
                                  stack=stack, upload=upload, reasoning=reasoning, api_key=api_key)


async def _run(db: CoordinatorDB, work: Path, origin: str, case: Case | None, *, headless: bool,
               chromium: str | None, wait: bool, check: PairedCheck | None, stack: AsyncExitStack,
               upload: UploadCheck | None, reasoning: StagePolicy, api_key: str | None) -> PracticeResult:
    migrate(db)
    firm = f"practice-{uuid4().hex}"
    profile = practice_web_profile(origin)
    preparer = PracticePreparer(firm, _RETURN, _SEAT, profile.profile_id)
    web_layout = layout(profile.profile_id, include_box3=case is None)
    originals = work / "originals"
    originals.mkdir(mode=0o700)
    protected = work / "private-worker"
    protected.mkdir(mode=0o700)
    storage = FilesystemOriginals(originals)
    authority = PracticeAuthority()
    steps: list[_Step] = []
    decisions: tuple[tuple[str, str], ...] = ()
    approved_plan: WorkerPlan | None = None
    selected_events: frozenset[UploadCommitted] = frozenset()
    generated = individual_sources(include_social_security_wages=case is None)
    approved_digests = {sha256(document.content).hexdigest() for document in generated}

    def committed_digest(event: UploadCommitted) -> str | None:
        if event.firm_id != firm:
            return None
        committed = runtime.intake._committed(event)
        if committed is not None:
            _, slot = committed
            digest = storage.checksum(event.firm_id, slot["object_id"])
            if digest in approved_digests and digest == slot["digest"]:
                return digest
        return None

    def classify(event: UploadCommitted, _hint: str | None) -> ClassifiedUpload:
        # Authorization derives from the bytes of the exact immutable outbox version.
        # Neither a user-chosen name nor a claimed return hint can classify a file.
        if event in selected_events and committed_digest(event) is not None:
            return ClassifiedUpload(_RETURN, "synthetic")
        return ClassifiedUpload()

    def verify(document):
        facts = verify_document(document)
        _say(f"  Verified document: {recognized_documents(facts) or 'unrecognized; leave for review'}")
        return facts

    def plan(item, view):
        nonlocal decisions, approved_plan
        selection = plan_individual(item, view, require_box_3=case is None)
        decisions = tuple((decision.section, decision.status) for decision in selection.decisions)
        _say("Section decisions from verified documents:")
        for decision in selection.decisions:
            _say(f"  {_LABELS.get(decision.section, decision.section)} — {decision.status.replace('_', ' ')}: {decision.reason}")
        if selection.plan is None:
            raise PreparationBlocked("practice-no-supported-fields")
        approved_plan = replace(selection.plan, confirm_completion=api_key is not None)
        return approved_plan

    expires: datetime | None = None

    def authorize(item):
        if expires is not None:
            return Authorization(item.input_revision, "reasoning", reasoning.identity, _CLASSES, expires)
        raise PreparationBlocked("practice-reasoning-not-authorized")

    @asynccontextmanager
    async def open_reasoning():
        if approved_plan is None:
            raise PreparationBlocked("practice-no-supported-fields")
        if api_key is None:
            yield _NextPermittedAction(web_layout, approved_plan, steps, 0 if headless else 0.8)
        else:
            try:
                async with open_provider(reasoning.identity, api_key=api_key) as provider:
                    yield _LiveNavigation(provider, approved_plan)
            except ProviderError as error:
                _say(f"{reasoning.identity.provider} reasoning unavailable: {error}")
                raise PreparationBlocked("provider-unavailable") from None

    @asynccontextmanager
    async def open_extraction():
        if api_key is None:
            yield FormDrivenExtraction(_FixtureExtraction(), _say)
        else:
            async with open_provider(reasoning.identity, api_key=api_key) as provider:
                yield FormDrivenExtraction(provider, _say)

    base_extraction_policy = reasoning if api_key else _policy("extraction")
    extraction_policy = replace(base_extraction_policy, stage="extraction", max_requests=2)
    extraction = ExtractionEnrollment(extraction_policy, "synthetic", open_extraction)
    runtime = FirmRuntime(db, FirmSite(
        firm, preparer.authenticate, authority, storage, classify, verify, extraction=extraction, reasoning_policy=reasoning,
        worker=WorkerEnrollment(_SEAT, _WORKER, reasoning,
            WebWorker(profile, web_layout, _Attestations(authority), protected, chromium=chromium, headed_practice=not headless),
            work / "seat.lock", plan, authorize, open_reasoning),
    ))
    runtime.ownership.register_machine(firm, _SEAT, _WORKER)
    runtime.ownership.qualify_machine(firm, _SEAT, profile.profile_id, TAX_YEAR, _STORE,
                                      shared_store=True, checkout_mode="firm")
    runtime.ownership.set_available(firm, _SEAT, True)
    # The authenticated intake roster is available before the first upload.
    with db.transaction() as connection:
        connection.execute("""INSERT INTO prep_returns (firm_id,return_id,profile_id,store_id,tax_year)
                              VALUES (%s,%s,%s,%s,%s)""", (firm, _RETURN, profile.profile_id, _STORE, TAX_YEAR))
        connection.execute("""INSERT INTO prep_return_directory
                              (firm_id,return_id,client_id,client_name,client_reference,return_type,product)
                              VALUES (%s,%s,%s,%s,%s,'individual','web')""",
                           (firm, _RETURN, _CLIENT, SUBJECT_NAME, SUBJECT_REFERENCE))
    companion = None
    if case is None:
        samples = work / "sample-documents"
        samples.mkdir(mode=0o700)
        paths = tuple(samples / document.filename for document in generated)
        for path, document in zip(paths, generated, strict=True):
            path.write_bytes(document.content)
        companion = await stack.enter_async_context(practice_companion(
            runtime=runtime, preparer=preparer, authority=authority, work=work,
            profile=profile, layout=web_layout, identity=_PERSON, chromium=chromium, accept_upload=True,
        ))
        mode = (f"{reasoning.identity.provider} extraction and tool reasoning ({reasoning.identity.model})"
                if api_key else "offline action policy")
        _say(f"Synthetic Sigma practice target: {origin} | {mode}")
        _say(f"Document inbox: {companion.origin}/clients")
        _say(f"Generated files: {samples}")
        _say(f"Choose {SUBJECT_NAME} and tax year {TAX_YEAR} in the inbox.")
        _say("Check the synthetic processing authorization, then drag the sample files into the inbox in one drop.")
        _say("Drop all six for the mixed case, or only the files you want the worker to use.")
        if upload is not None:
            await upload(companion.page, paths)
        batch = await companion.wait_for_batch()
    else:
        w2_a, w2_b, form_k, summary, form_nec, receipts = generated
        documents = {"wages": (w2_b, w2_a), "mixed": (receipts, w2_b, form_nec, summary, w2_a, form_k),
                     "rideshare": (summary, receipts, form_k)}[case]
        scope = IngestScope(firm, "practice-intake", frozenset({_RETURN}))
        batch_id = "generated-documents"
        files = tuple(BatchFile(slot=f"input-{number}", filename=document.filename, media_type=document.media_type)
                      for number, document in enumerate(documents, 1))
        _say(f"Offline synthetic Sigma target: {origin} | case: {case} | no external model or tax software")
        _say(f"Committing {len(files)} generated originals with no destination section or return hint in the upload.")
        runtime.intake.declare(scope, BatchManifest(batch_id=batch_id, approved_for_ai=True, files=files),
                               policy=extraction.recipient)
        for file, document in zip(files, documents, strict=True):
            batch = runtime.intake.put(scope, batch_id, file.slot, file.media_type, document.content)

    events = _upload_events(firm, batch)
    selected_events = frozenset(events)
    digests = [committed_digest(event) for event in events]
    _require(all(digests), "Only this run's generated sample files are supported; no draft was prepared.")
    _require(len(set(digests)) == len(events), "Drop each sample once; no draft was prepared.")
    expected_inputs = {(event.upload_id, event.version_id, digest) for event, digest in zip(events, digests, strict=True)}
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)

    # Register every receipt before processing any extraction so the sealed UI
    # drop freezes as one revision. All subsequent operations use the real runtime.
    _require(set(runtime.intake.committed_events(firm_id=firm)) == selected_events,
             "The committed outbox did not cover exactly the submitted originals.")
    _say(f"Processing one sealed drop of {len(events)} verified synthetic originals.")
    for event in events:
        _require(runtime.dispatch.consume(event), "The coordinator did not accept a committed practice receipt.")
    _require(await runtime.dispatch_once(concurrency=1) == len(events), "Committed practice originals did not dispatch.")
    if case is not None:
        _require(await runtime.dispatch_once(concurrency=1) == 0, "Committed originals unexpectedly replayed.")
    worker = runtime.worker()
    job = await worker.run_once()
    _require(job is not None, "No preparation job was assigned from the committed documents.")
    if case is not None:
        _require(await worker.run_once() is None, "More than one preparation job was dispatched for the batch.")

    with db.transaction() as connection:
        terminal = connection.execute("SELECT state,snapshot FROM prep_jobs WHERE (firm_id,job_id)=(%s,%s)",
                                      (firm, job)).fetchone()
        native = connection.execute("SELECT draft_key FROM prep_returns WHERE (firm_id,return_id)=(%s,%s)",
                                    (firm, _RETURN)).fetchone()
        journal = connection.execute("SELECT event FROM prep_owner_events WHERE (firm_id,return_id)=(%s,%s) ORDER BY event_order",
                                     (firm, _RETURN)).fetchall()
        verified = connection.execute("SELECT result FROM prep_verifications WHERE (firm_id,job_id)=(%s,%s)",
                                      (firm, job)).fetchone()
        mutations = connection.execute("SELECT operation->>'kind' AS kind,state FROM prep_steps WHERE (firm_id,job_id)=(%s,%s)",
                                       (firm, job)).fetchall()
    state, key = terminal["state"], native["draft_key"]
    _require(key and verified, f"The worker did not produce a safely reopened draft: {state}; journal: {journal}")
    inputs = terminal["snapshot"]["inputs"]
    _require({(source["upload_id"], source["version_id"], source["sha256"]) for source in inputs} == expected_inputs,
             "The worker did not prepare one revision covering the complete sealed drop.")
    _require(journal and journal[-1]["event"] == "released", f"The practice ownership grant did not release: {journal}")
    _say(f"Worker closed and released its draft after save/reopen verification: {state}.")
    issues = tuple(verified["result"]["issues"])
    for issue in issues:
        _say(f"  Saved verification needs review: {issue}")
    saved = await _read_saved(profile, chromium, key)
    _display(saved)
    revision = max(int(source["artifact_id"]) for source in inputs)
    view = runtime.tax.read_revision(TaxAccess(firm_id=firm, return_id=_RETURN, actor_id="practice-review", scopes={"read"}),
                                     revision)
    result = PracticeResult(state, decisions, issues, saved)
    landing = "w2" if saved.wages else "rideshare"
    if case is not None:
        _check_run(case, result, steps, mutations, view)
        _say("Verified: supported sections saved; skipped and unresolved entries were left untouched.")
    else:
        _show_precision(view)
    if companion is not None:
        companion.saved = key, landing
        await companion.show_review(_CLIENT, TAX_YEAR, check)
        if check is None:
            _say("Close the companion window or press Ctrl+C when finished.")
            await companion.wait_closed()
    elif wait:
        if headless:
            _say(f"Manual practice draft: {origin}/drafts/{key}/{landing}")
            await _pause()
        else:
            await paired_review(runtime=runtime, preparer=preparer, authority=authority, work=work,
                                profile=profile, layout=web_layout, identity=_PERSON, draft_key=key,
                                landing=landing, client_id=_CLIENT, chromium=chromium, pause=_pause, check=check)
    return result


def _upload_events(firm: str, batch: BatchView) -> tuple[UploadCommitted, ...]:
    _require(batch.state == "sealed", "No sample files were committed. Restart for a fresh drop.")
    _require(batch.approved_for_ai, "This drop was not authorized. Restart, check the authorization, then drop the samples.")
    events = []
    for slot in batch.files:
        if slot.state == "uploaded":
            _require(slot.upload_id and slot.version_id, "Committed original has no immutable identity.")
            events.append(UploadCommitted(firm, slot.upload_id, slot.version_id))
    return tuple(events)


@contextmanager
def _practice_database(directory: Path) -> Iterator[CoordinatorDB]:
    socket = os.environ.get("SIGMA_TEST_POSTGRES_SOCKET")
    if not socket:
        with local_postgres(directory, bin_dir=os.environ.get("SIGMA_PRACTICE_PG_BIN")) as db:
            yield db
        return
    path = Path(socket)
    if not path.is_absolute() or not path.is_dir():
        raise PracticeError("SIGMA_TEST_POSTGRES_SOCKET must name an absolute disposable PostgreSQL socket directory.")
    schema = f"sigma_practice_{uuid4().hex}"

    def connect(**options):
        return psycopg.connect(host=str(path), port=int(os.environ.get("SIGMA_TEST_POSTGRES_PORT", "5432")),
                               dbname="postgres", row_factory=dict_row, connect_timeout=2, **options)

    with connect(autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield CoordinatorDB(lambda: connect(options=f"-c search_path={schema},pg_catalog"))
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use the local action policy instead of a model.")
    parser.add_argument("--provider", choices=tuple(_LIVE_PROVIDERS), help="Select the live provider if multiple are configured.")
    parser.add_argument("--chromium", help="Path to a full Chromium browser.")
    args = parser.parse_args()
    if args.provider and args.offline:
        parser.error("--provider cannot be used with --offline")
    try:
        live = None if args.offline else _live_settings(args.provider)
    except PracticeError as error:
        parser.error(str(error))
    try:
        with TemporaryDirectory(prefix="sigma-practice-") as temporary:
            directory = Path(temporary)
            with _practice_database(directory) as db:
                asyncio.run(run_interactive(db, directory, chromium=args.chromium, live=live))
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, psycopg.Error, PlaywrightError) as error:
        _say(f"Practice run failed: {error}")
        return 1
    return 0
