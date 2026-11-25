"""Local draft tools for reviewed, bounded, native HTML form layouts."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError, Locator
from pydantic import ConfigDict, StrictBool, StrictInt

from .native_plan import DraftPlan, FieldPlan, RecordPlan
from .sigma_web import WebBlocked, WebDraft
from .tool_contract import (
    Action, ActionOutcome, Choice, JobScope, MutationDetails, MutationPermit, Observation, Verification, VerifiedReadback,
)
from .tools import ToolHostError
from .web_controls import ControlBlocked, ControlKind, enter_control


@dataclass(frozen=True)
class WebField:
    __pydantic_config__ = ConfigDict(extra="forbid")

    key: str
    label: str
    name: str
    kind: Literal["text", "decimal"] = "text"
    required: StrictBool = False
    column: str | None = None
    control: Literal["input", "date", "select"] = "input"


@dataclass(frozen=True)
class WebRecords:
    __pydantic_config__ = ConfigDict(extra="forbid")

    table: str
    key_attribute: str
    identity_keys: tuple[str, ...]
    add: str
    edit: str
    back: str
    actions_column: str
    row_limit: StrictInt
    navigation_labels: tuple[str, ...] = ()
    new_segment: str = "new"
    new_dialog: str | None = None
    edit_dialog: str | None = None
    # Exact native values that may be persisted without an assigned write.
    approved_defaults: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WebLink:
    __pydantic_config__ = ConfigDict(extra="forbid")

    label: str
    destination: str
    action: Literal["open_section", "open_detail"]


@dataclass(frozen=True)
class WebSection:
    __pydantic_config__ = ConfigDict(extra="forbid")

    key: str
    path: str  # Exact, same-draft path template with one {draft_key} placeholder.
    fields: tuple[WebField, ...]
    save: str
    links: tuple[WebLink, ...] = ()
    records: WebRecords | None = None
    dialog: str | None = None


@dataclass(frozen=True)
class WebLayout:
    __pydantic_config__ = ConfigDict(extra="forbid")

    profile_id: str
    sections: tuple[WebSection, ...]


@dataclass(frozen=True)
class _Row:
    key: str
    values: dict[str, str]
    link: Locator = field(compare=False)


@dataclass(frozen=True)
class _Screen:
    section: WebSection
    row: str | None
    root: Locator = field(compare=False)
    rows: dict[str, _Row] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
    editable: frozenset[str] = frozenset()
    form: Locator | None = field(default=None, compare=False)
    button: Locator | None = field(default=None, compare=False)
    controls: dict[str, Locator] = field(default_factory=dict, compare=False)
    control_kinds: dict[str, ControlKind] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class _Pending:
    section: str
    row: str | None
    before: dict[str, str]
    intended: dict[str, str]


@dataclass(frozen=True)
class _Operation:
    choice: Choice
    link: Locator | None = None
    destination: str | None = None
    fields: tuple[FieldPlan, ...] = ()
    record: RecordPlan | None = None
    row: str | None = None


class _UnsafeScreen(Exception):
    pass


_NATIVE_KEY = re.compile(r"[A-Za-z0-9_-]{1,120}\Z")
_REJECTION = (WebBlocked, PlaywrightError, ControlBlocked, _UnsafeScreen)


def _same(kind: str, left: str, right: str) -> bool:
    if kind == "decimal" and left and right:
        try:
            one, two = Decimal(left), Decimal(right)
        except InvalidOperation:
            return False
        return one.is_finite() and two.is_finite() and one == two
    return left == right


def _normalized(kind: str, value: str) -> str | None:
    if kind != "decimal" or value == "":
        return value
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if not number:
        return "0"
    sign, digits, exponent = number.as_tuple()
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
    # Avoid context-dependent rounding and exponent expansion when normalizing decimals.
    try:
        normalized = Decimal((sign, digits[:end], exponent + len(digits) - end))
    except InvalidOperation:
        return str(number)
    if -2040 < normalized.adjusted() < 2040:
        plain = format(normalized, "f")
        if len(plain) <= 2048:
            return plain
    return str(normalized)


async def _unique_visible(control: Locator) -> None:
    if await control.count() != 1 or not await control.is_visible():
        raise _UnsafeScreen


class WebActions:
    """One pre-opened draft. Provider tool calls contain refs, never native values or locators."""

    def __init__(self, session: WebDraft, scope: JobScope, layout: WebLayout, plan: DraftPlan):
        if layout.profile_id != scope.profile_id or session.profile.profile_id != scope.profile_id:
            raise ValueError("Web layout does not match the scoped browser profile.")
        if not _NATIVE_KEY.fullmatch(scope.draft_key):
            raise ValueError("Unsupported web draft key.")
        self._session, self._scope = session, scope
        self._identity = plan.identity
        self._sections = {section.key: section for section in layout.sections}
        # Do not retain caller-owned dictionaries inside the validated job plan.
        self._plan = {section.section_key: section for section in plan.model_copy(deep=True).sections}
        if len(self._sections) != len(layout.sections) or not self._plan.keys() <= self._sections.keys():
            raise ValueError("Ambiguous or unknown web section.")
        paths = set()
        for section in layout.sections:
            path = section.path
            if (path.count("{draft_key}") != 1 or not path.startswith("/") or path.startswith("//")
                    or path.endswith("/") or re.search(r"[\\?#%]", path)
                    or any(segment in {".", ".."} for segment in path.split("/")) or path in paths):
                raise ValueError("Web section must identify one exact draft route.")
            paths.add(path)
            fields = {control.key: control for control in section.fields}
            if (len(fields) != len(section.fields) or len({f.label for f in section.fields}) != len(fields)
                    or len({f.name for f in section.fields}) != len(fields)
                    or any(link.destination not in self._sections or link.destination == section.key
                           for link in section.links)):
                raise ValueError("Ambiguous web controls.")
            records = section.records
            if records is not None and (
                not 1 <= records.row_limit <= 256 or not _NATIVE_KEY.fullmatch(records.new_segment)
                or not records.identity_keys or len(set(records.identity_keys)) != len(records.identity_keys)
                or any(key not in fields or fields[key].kind != "text" for key in records.identity_keys)
                or not re.fullmatch(r"data-[a-z0-9-]+", records.key_attribute)
                or len({*(f.column or f.label for f in section.fields), records.actions_column}) != len(fields) + 1
                or len({records.add, records.edit, *records.navigation_labels}) != len(records.navigation_labels) + 2
            ):
                raise ValueError("Record lists require reviewed columns, stable keys and a bounded identity.")
            if records is not None:
                defaults = dict(records.approved_defaults)
                if (len(defaults) != len(records.approved_defaults) or not defaults.keys() <= fields.keys()
                        or any(not value.strip() or len(value) > 2048 or _normalized(fields[key].kind, value) is None
                               for key, value in defaults.items())):
                    raise ValueError("Record defaults must approve exact nonblank values of reviewed fields.")
            assignment = self._plan.get(section.key)
            if assignment is None:
                continue
            if ((records is None and assignment.records) or (records is not None and assignment.fields)
                    or any(set(row.identity) != set(records.identity_keys) for row in assignment.records)
                    or any(row.external_key is not None and not _NATIVE_KEY.fullmatch(row.external_key)
                           for row in assignment.records)):
                raise ValueError("Assignment does not match the reviewed record scope.")
            for binding in (*assignment.fields, *(f for row in assignment.records for f in row.fields)):
                if binding.field_key not in fields or binding.kind != fields[binding.field_key].kind:
                    raise ValueError("Binding does not match a reviewed field.")
        self._paths = {key: section.path.replace("{draft_key}", scope.draft_key)
                       for key, section in self._sections.items()}
        # Offer only routes between assigned sections and from the landing page.
        # A section merely linking back to a required page is not a necessary visit.
        self._transitions: set[tuple[str, str]] = set()
        landing = next(section.context for section in session.profile.sections
                       if section.route == session.profile.landing_section)
        if landing not in self._sections:
            raise ValueError("The reviewed layout is missing its landing section.")
        for start in {landing, *self._plan}:
            routes = self._routes_from(start)
            for destination in self._plan:
                if destination not in routes:
                    if start == landing:
                        raise ValueError("The reviewed layout cannot reach an assigned section.")
                    continue
                origin = start
                for link in routes[destination]:
                    self._transitions.add((origin, link.destination))
                    origin = link.destination
        self._active: tuple[str, str, RecordPlan] | None = None
        self._pending: _Pending | None = None
        self._bound: dict[str, str] = {}
        self._ledgers: dict[str, dict[str, _Row]] = {}
        self._saved: dict[str, dict[str, str]] = {}
        self._last: tuple[_Screen, Observation] | None = None
        self._steps: set[str] = set()
        self._progress: dict[str, Literal["completed", "blocked"]] = {}
        self._read_only: set[str] = set()
        self._creation_failures: dict[str, str] = {}
        self._uncertain = False

    async def _link(self, root: Locator, label: str, path: str) -> Locator:
        link = root.get_by_role("link", name=label, exact=True)
        await _unique_visible(link)
        if (await link.get_attribute("href") != path or await link.get_attribute("download") is not None
                or await link.get_attribute("target") not in {None, "", "_self"}):
            raise _UnsafeScreen
        return link

    async def _read_rows(self, root: Locator, section: WebSection, path: str) -> dict[str, _Row]:
        records = section.records
        assert records is not None
        table = root.get_by_role("table", name=records.table, exact=True)
        await _unique_visible(table)
        if await root.get_by_role("table").count() != 1:
            raise _UnsafeScreen
        rows = table.get_by_role("row", include_hidden=True)
        count = await rows.count()
        if not 1 <= count <= records.row_limit + 1:
            raise _UnsafeScreen
        # This profile promises a single exhaustive DOM list. Additional link/button
        # controls could be paging or filtering; their presence invalidates absence.
        links = {records.add: 1, records.edit: count - 1, **dict.fromkeys(records.navigation_labels, 1)}
        if (await root.get_by_role("link", include_hidden=True).count() != sum(links.values())
                or await root.get_by_role("button", include_hidden=True).count()
                or await root.get_by_role("status").count() or await root.get_by_role("alert").count()
                or await root.locator("form").count()):
            raise _UnsafeScreen
        for label, expected in links.items():
            if await root.get_by_role("link", name=label, exact=True).count() != expected:
                raise _UnsafeScreen
        header = [*(f.column or f.label for f in section.fields), records.actions_column]
        if (await rows.nth(0).get_by_role("columnheader").all_text_contents() != header
                or not await rows.nth(0).is_visible()):
            raise _UnsafeScreen
        result = {}
        for index in range(1, count):
            row = rows.nth(index)
            key = await row.get_attribute(records.key_attribute)
            cells = row.get_by_role("cell")
            if (not key or not _NATIVE_KEY.fullmatch(key) or key in result or key == records.new_segment
                    or not await row.is_visible() or await cells.count() != len(header)):
                raise _UnsafeScreen
            values = {}
            for i, f in enumerate(section.fields):
                cell = cells.nth(i)
                if not await cell.is_visible() or await cell.locator("*").count():
                    raise _UnsafeScreen
                value = await cell.text_content()
                if value is None or len(value) > 2048:
                    raise _UnsafeScreen
                values[f.key] = value
            if any(not values[key].strip() for key in records.identity_keys):
                raise _UnsafeScreen
            link = await self._link(cells.nth(len(header) - 1), records.edit, f"{path}/{key}")
            result[key] = _Row(key, values, link)
        return result

    async def _read(self) -> _Screen:
        section_key, row = await self._session.require_draft(self._identity, self._scope.draft_key)
        section = self._sections.get(section_key)
        page = self._session.page
        if section is None or len(page.frames) != 1:
            raise _UnsafeScreen
        records = section.records
        if row is not None and (not records or not _NATIVE_KEY.fullmatch(row)):
            raise _UnsafeScreen
        path = self._paths[section_key] + (f"/{row}" if row is not None else "")
        if urlsplit(page.url).path != path:
            raise _UnsafeScreen
        dialog = section.dialog
        if records and row is not None:
            dialog = records.new_dialog if row == records.new_segment else records.edit_dialog
        if await page.get_by_role("dialog").count() != (1 if dialog else 0):
            raise _UnsafeScreen
        root = page.get_by_role("dialog", name=dialog, exact=True) if dialog else page.get_by_role("main")
        await _unique_visible(root)
        if records and row is None:
            return _Screen(section, row, root, rows=await self._read_rows(root, section, path))

        form = root.locator("form")
        await _unique_visible(form)
        if records and (await form.get_attribute(records.key_attribute) != (None if row == records.new_segment else row)):
            raise _UnsafeScreen
        button = form.get_by_role("button", name=section.save, exact=True)
        await _unique_visible(button)
        # Only the reviewed submission and fields may be part of this form.
        if (await form.get_by_role("button", include_hidden=True).count() != 1
                or await form.locator("input, textarea, select, [form]").count() != len(section.fields)
                or not await button.is_enabled()):
            raise _UnsafeScreen
        values, controls, kinds, editable = {}, {}, {}, set()
        for f in section.fields:
            selector = {"input": "input, textarea", "date": 'input[type="date"]', "select": "select"}[f.control]
            control = form.get_by_label(f.label, exact=True).and_(form.locator(selector))
            await _unique_visible(control)
            type_ = await control.get_attribute("type")
            allowed_types = {None, "text", "number"} if f.control == "input" else {"date", None}
            if (type_ not in allowed_types or not await control.is_enabled()
                    or await control.get_attribute("name") != f.name
                    or (await control.get_attribute("required") is not None) != f.required):
                raise _UnsafeScreen
            kinds[f.key] = "select" if f.control == "select" else "input"
            values[f.key], controls[f.key] = await control.input_value(), control
            if len(values[f.key]) > 2048:
                raise _UnsafeScreen
            if await control.is_editable():
                editable.add(f.key)
        return _Screen(section, row, root, values=values, editable=frozenset(editable),
                       form=form, button=button, controls=controls, control_kinds=kinds)

    def _same_values(self, section: WebSection, one: dict[str, str], two: dict[str, str]) -> bool:
        return all(_same(f.kind, one[f.key], two[f.key]) for f in section.fields)

    def _same_rows(self, section: WebSection, one: dict[str, _Row], two: dict[str, _Row]) -> bool:
        return one.keys() == two.keys() and all(
            self._same_values(section, row.values, two[key].values) for key, row in one.items()
        )

    def _match(self, screen: _Screen, record: RecordPlan) -> tuple[_Row | None, str | None]:
        matches = [row for row in screen.rows.values() if all(row.values[k] == v for k, v in record.identity.items())]
        expected = self._bound.get(record.source_record_id, record.external_key)
        if len(matches) > 1:
            return None, "ambiguous-record"
        if expected is not None and (not matches or matches[0].key != expected):
            return None, "record-identity-conflict"
        return (matches[0] if matches else None), None

    def _can_create(self, section: WebSection, record: RecordPlan, row_count: int) -> bool:
        records = section.records
        assert records is not None
        defaults = dict(records.approved_defaults)
        usable = {f.field_key for f in record.fields
                  if not f.preparer_owned and (f.may_enter(defaults.get(f.field_key, ""))
                                               or f.matches(defaults.get(f.field_key, "")))}
        return (record.allow_create and row_count < records.row_limit and bool(usable)
                and record.source_record_id not in self._creation_failures
                and all(not defaults.get(key) or defaults[key] == value for key, value in record.identity.items())
                and all(f.field_key not in defaults or f.field_key in usable for f in record.fields)
                and all(not f.required or f.key in usable or f.key in record.identity or f.key in defaults
                        for f in section.fields))

    def _binding_progress(self, binding: FieldPlan, value: str, *, saved: bool) -> None:
        if binding.matches(value):
            if saved:
                self._progress[binding.binding_id] = "completed"
            else:
                self._progress.pop(binding.binding_id, None)
        elif binding.binding_id in self._read_only or not binding.may_enter(value):
            self._progress[binding.binding_id] = "blocked"
        else:
            self._progress.pop(binding.binding_id, None)

    async def _operations(self, screen: _Screen, obs_id: str) -> tuple[_Operation, ...]:
        section = screen.section
        key = section.key
        path = self._paths[key]
        assignment = self._plan.get(key)
        pending = self._pending
        operations = []

        def offer(name, semantic, *, mutation=None, refs=None, **op):
            action = Action(name=name, arguments={"observation_id": obs_id, **(refs or {})})
            operations.append(_Operation(Choice(action=action, semantic_key=semantic, mutation=mutation), **op))

        if pending and ((pending.section, pending.row) != (key, screen.row)
                        or not self._same_values(section, pending.intended, screen.values)):
            raise _UnsafeScreen
        if pending is None and screen.row is None:
            for link in section.links:
                if (key, link.destination) in self._transitions:
                    destination = self._paths[link.destination]
                    locator = await self._link(screen.root, link.label, destination)
                    offer(link.action, link.destination, refs={"control_ref": f"control-{len(operations)}"},
                          link=locator, destination=link.destination)

        if section.records and screen.row is None:
            original = self._ledgers.setdefault(key, screen.rows)
            if not self._same_rows(section, original, screen.rows):
                raise _UnsafeScreen
            if assignment:
                offer("list_records", key, refs={"list_ref": "records"})
                for record in assignment.records:
                    row, issue = self._match(screen, record)
                    blocked = issue is not None or (row is None and not self._can_create(section, record, len(screen.rows)))
                    for binding in record.fields:
                        if blocked:
                            self._progress[binding.binding_id] = "blocked"
                        elif row is not None:
                            self._binding_progress(binding, row.values[binding.field_key], saved=True)
                        else:
                            self._progress.pop(binding.binding_id, None)
                    if blocked:
                        continue
                    row_key = row.key if row else section.records.new_segment
                    locator = row.link if row else await self._link(screen.root, section.records.add, f"{path}/{row_key}")
                    details = MutationDetails(
                        before={"rows": {k: item.values for k, item in screen.rows.items()}},
                        intended={"recordKey": row.key if row else None, "identity": record.identity},
                    )
                    offer("ensure_record", key, refs={"list_ref": "records", "source_record_id": record.source_record_id},
                          mutation=details, link=locator, record=record, row=row_key)
        else:
            active = self._active
            if section.records:
                if (active is None or active[:2] != (key, screen.row)
                        or any(screen.values[field] != value for field, value in active[2].identity.items())):
                    raise _UnsafeScreen
                fields = active[2].fields
                if pending is None:
                    locator = await self._link(screen.root, section.records.back, path)
                    offer("open_detail", key, refs={"control_ref": f"control-{len(operations)}"},
                          link=locator, destination=key)
            else:
                fields = assignment.fields if assignment else ()
            supported = False
            writes = []
            for binding in fields:
                before = screen.values[binding.field_key]
                if binding.field_key in screen.editable:
                    self._read_only.discard(binding.binding_id)
                else:
                    self._read_only.add(binding.binding_id)
                previously_saved = pending is None or (
                    (section.records is None or screen.row != section.records.new_segment)
                    and binding.matches(pending.before[binding.field_key])
                )
                self._binding_progress(binding, before, saved=previously_saved)
                supported |= binding.matches(before)
                if binding.field_key in screen.editable and binding.may_enter(before):
                    writes.append(binding)
            if len(writes) == 1:
                binding, = writes
                offer("set_field", binding.field_key, refs={
                    "control_ref": f"control-{len(operations)}", "binding_id": binding.binding_id,
                    "fact_ids": binding.fact_ids,
                }, mutation=MutationDetails(before=screen.values[binding.field_key], intended=binding.value),
                      fields=(binding,))
            elif writes:
                offer(
                    "set_fields", key, refs={"fields": tuple(
                        {"binding_id": binding.binding_id, "fact_ids": binding.fact_ids} for binding in writes
                    )}, mutation=MutationDetails(
                        before={binding.binding_id: screen.values[binding.field_key] for binding in writes},
                        intended={binding.binding_id: binding.value for binding in writes},
                    ), fields=tuple(writes),
                )
            if pending and supported and all(not f.required or screen.values[f.key].strip() for f in section.fields):
                before = {"recordKey": screen.row, "values": pending.before}
                intended = {"values": pending.intended}
                if section.records:
                    assert active is not None
                    intended["sourceRecordId"] = active[2].source_record_id
                    intended["identity"] = active[2].identity
                    before["rows"] = {k: row.values for k, row in self._ledgers[key].items()}
                offer("save_draft", key, refs={"scope_ref": "form"},
                      mutation=MutationDetails(before=before, intended=intended))
        if len(operations) > 256:
            raise _UnsafeScreen
        return tuple(operations)

    def _observation(self, screen: _Screen, obs_id: str, operations: tuple[_Operation, ...]) -> Observation:
        return Observation(
            scope=self._scope, observation_id=obs_id, context=screen.section.key,
            choices=tuple(op.choice for op in operations),
            completed_binding_ids=tuple(key for key, state in sorted(self._progress.items()) if state == "completed"),
            blocked_binding_ids=tuple(key for key, state in sorted(self._progress.items()) if state == "blocked"),
            pending_save=self._pending is not None,
        )

    async def _snapshot(self) -> tuple[_Screen, Observation, tuple[_Operation, ...]]:
        if self._uncertain:
            raise ToolHostError("reconciliation-required")
        screen = await self._read()
        same_screen = self._last is not None and self._last[0] == screen
        obs_id = self._last[1].observation_id if same_screen else uuid4().hex
        operations = await self._operations(screen, obs_id)
        observation = self._observation(screen, obs_id, operations)
        if same_screen and observation != self._last[1]:
            obs_id = uuid4().hex
            operations = await self._operations(screen, obs_id)
            observation = self._observation(screen, obs_id, operations)
        self._last = screen, observation.model_copy(deep=True)
        return screen, observation, operations

    async def observe(self) -> Observation:
        try:
            return (await self._snapshot())[1]
        except _REJECTION:
            self._last = None
            raise ToolHostError("stale-observation") from None

    async def _follow(self, link: Locator, section_key: str, row: str | None = None) -> _Screen:
        path = self._paths[section_key] + (f"/{row}" if row is not None else "")
        await self._session.follow(self._identity, self._scope.draft_key, link, path=path)
        after = await self._read()
        if (after.section.key, after.row) != (section_key, row):
            raise _UnsafeScreen
        return after

    async def _fill(self, screen: _Screen, key: str, value: str) -> _Screen:
        if key not in screen.editable:
            raise _UnsafeScreen
        await enter_control(screen.controls[key], screen.control_kinds[key], value)
        after = await self._read()
        if ((after.section.key, after.row) != (screen.section.key, screen.row)
                or not self._same_values(screen.section, {**screen.values, key: value}, after.values)):
            raise _UnsafeScreen
        return after

    async def _ensure(self, screen: _Screen, op: _Operation) -> bool:
        section = screen.section
        assert section.records is not None and op.record is not None and op.row is not None and op.link is not None
        form = await self._follow(op.link, section.key, op.row)
        if op.row == section.records.new_segment:
            defaults = dict(section.records.approved_defaults)
            edits = {key: value for key, value in op.record.identity.items() if form.values[key] != value}
            usable = {
                binding.field_key for binding in op.record.fields if not binding.preparer_owned and (
                    binding.matches(form.values[binding.field_key]) or
                    (binding.field_key in form.editable and binding.may_enter(form.values[binding.field_key]))
                )
            }
            issue = None
            if any(value != defaults.get(key, "") for key, value in form.values.items()):
                issue = "unexplained-record-defaults"
            elif (not edits.keys() <= form.editable or not usable
                  or any(f.required and not form.values[f.key] and f.key not in edits and f.key not in usable
                         for f in section.fields)):
                issue = "record-fields-unavailable"
            if issue is not None:
                # No field fill or submission has been dispatched. Prove the
                # list is unchanged before acknowledging the no-op.
                back = await self._link(form.root, section.records.back, self._paths[section.key])
                restored = await self._follow(back, section.key)
                if not self._same_rows(section, screen.rows, restored.rows):
                    raise _UnsafeScreen
                self._creation_failures[op.record.source_record_id] = issue
                return False
            before = form.values
            for key, value in edits.items():
                form = await self._fill(form, key, value)
            self._pending = _Pending(section.key, op.row, before, form.values)
        else:
            if not self._same_values(section, screen.rows[op.row].values, form.values):
                raise _UnsafeScreen
            self._bound[op.record.source_record_id] = op.row
        self._active = section.key, op.row, op.record
        return True

    async def _save(self, screen: _Screen) -> None:
        section = screen.section
        path = self._paths[section.key]
        assert screen.form is not None and screen.button is not None
        action_path = path + (f"/{screen.row}" if screen.row is not None else "")
        expected_fields = tuple((f.name, screen.values[f.key]) for f in section.fields)
        await self._session.submit(self._identity, self._scope.draft_key, screen.form, screen.button,
                                   path=action_path, result_path=path, expected_fields=expected_fields)
        after = await self._read()
        if (after.section.key, after.row) != (section.key, None):
            raise _UnsafeScreen
        if section.records:
            assert self._active is not None
            record = self._active[2]
            bound, issue = self._match(after, record)
            original = self._ledgers[section.key]
            if (issue or bound is None or not self._same_values(section, screen.values, bound.values)
                    or (screen.row == section.records.new_segment and bound.key in original)
                    or (screen.row != section.records.new_segment and bound.key != screen.row)
                    or not self._same_rows(section, {k: row for k, row in original.items() if k != bound.key},
                                           {k: row for k, row in after.rows.items() if k != bound.key})):
                raise _UnsafeScreen
            self._bound[record.source_record_id] = bound.key
            self._ledgers[section.key] = after.rows
        else:
            if not self._same_values(section, screen.values, after.values):
                raise _UnsafeScreen
            self._saved[section.key] = after.values
        self._active = self._pending = None

    async def execute(self, action: Action, permit: MutationPermit | None) -> ActionOutcome:
        if self._last is None or self._last[1].observation_id != action.observation_id:
            raise ToolHostError("stale-observation")
        try:
            screen, current, operations = await self._snapshot()
        except _REJECTION:
            self._last = None
            raise ToolHostError("stale-observation") from None
        if current.observation_id != action.observation_id:
            raise ToolHostError("stale-observation")
        op = next((op for op in operations if op.choice.action == action), None)
        if op is None:
            raise ToolHostError("unauthorized-action")
        if action.mutates and permit is None:
            raise ToolHostError("missing-permit")
        if permit is not None:
            if permit.scope != self._scope:
                raise ToolHostError("scope-mismatch")
            if not action.mutates or permit.action != action or permit.details != op.choice.mutation:
                raise ToolHostError("invalid-permit")
            if permit.step_id in self._steps:
                raise ToolHostError("duplicate-step")
            self._steps.add(permit.step_id)
            # Cancellation or failed readback must not permit another write in this session.
            self._uncertain = True
        self._last = None
        try:
            if action.name == "list_records":
                return ActionOutcome(status="unchanged")
            if op.destination is not None:
                assert op.link is not None
                await self._follow(op.link, op.destination)
                self._active = None
            elif op.record is not None:
                if not await self._ensure(screen, op):
                    self._uncertain = False
                    return ActionOutcome(status="unchanged")
            elif op.fields:
                before = self._pending.before if self._pending else screen.values
                after = screen
                for binding in op.fields:
                    after = await self._fill(after, binding.field_key, binding.value)
                self._pending = _Pending(screen.section.key, screen.row, before, after.values)
            else:
                await self._save(screen)
        except _REJECTION:
            if action.mutates:
                return ActionOutcome(status="uncertain")
            raise ToolHostError("stale-observation") from None
        self._uncertain = False
        return ActionOutcome(status="applied")

    def _routes_from(self, start: str) -> dict[str, tuple[WebLink, ...]]:
        routes: dict[str, tuple[WebLink, ...]] = {start: ()}
        queue = [start]
        for key in queue:
            for link in self._sections[key].links:
                if link.destination not in routes:
                    routes[link.destination] = (*routes[key], link)
                    queue.append(link.destination)
        return routes

    async def _travel(self, start: _Screen, destination: str) -> _Screen:
        # Follow reviewed native links; never navigate by an invented URL.
        route = self._routes_from(start.section.key).get(destination)
        if route is None:
            raise _UnsafeScreen
        screen = start
        for link in route:
            control = await self._link(screen.root, link.label, self._paths[link.destination])
            screen = await self._follow(control, link.destination)
        return screen

    async def verify(self) -> Verification:
        self._last = None
        verified, readbacks, issues = [], [], set()

        def compare(section, fields, values, *, source_record_id=None, native_row_key=None):
            for binding in fields:
                current = values[binding.field_key]
                matches = binding.matches(current)
                readbacks.append(VerifiedReadback(
                    scope=self._scope, section_key=section.key, field_key=binding.field_key,
                    binding_id=binding.binding_id, fact_ids=binding.fact_ids, source_record_id=source_record_id,
                    native_row_key=native_row_key, value_kind=binding.kind,
                    normalized_value=_normalized(binding.kind, current), matches_intended=matches,
                ))
                if matches:
                    verified.append(binding.binding_id)
                else:
                    issues.add("field-not-saved" if binding.may_enter(current) else "field-conflict")

        try:
            await self._session.require_draft(self._identity, self._scope.draft_key)
            if self._uncertain:
                return Verification(scope=self._scope, issues=("reconciliation-required",))
            if self._pending is not None:
                issues.add("unsaved-changes")
            if await self._session.reopen(self._identity, self._scope.draft_key) != self._scope.draft_key:
                raise _UnsafeScreen
            self._pending = self._active = None
            screen = await self._read()
            for key, assignment in self._plan.items():
                screen = await self._travel(screen, key)
                section = screen.section
                if section.records is None:
                    if key in self._saved and not self._same_values(section, self._saved[key], screen.values):
                        issues.add("saved-values-changed")
                    compare(section, assignment.fields, screen.values)
                    continue
                if key in self._ledgers and not self._same_rows(section, self._ledgers[key], screen.rows):
                    issues.add("record-list-changed")
                for record in assignment.records:
                    row, issue = self._match(screen, record)
                    if row is None:
                        issues.add(issue or self._creation_failures.get(record.source_record_id, "record-not-saved"))
                        continue
                    form = await self._follow(row.link, key, row.key)
                    if not self._same_values(section, row.values, form.values):
                        issues.add("record-readback-mismatch")
                    else:
                        compare(section, record.fields, form.values,
                                source_record_id=record.source_record_id, native_row_key=row.key)
                    link = await self._link(form.root, section.records.back, self._paths[key])
                    screen = await self._follow(link, key)
        except _REJECTION:
            issue = "reconciliation-required" if self._uncertain else "reopen-unverified"
            return Verification(scope=self._scope, issues=(issue,))
        return Verification(scope=self._scope, verified_binding_ids=tuple(verified),
                            issues=tuple(sorted(issues)), readbacks=tuple(readbacks))
