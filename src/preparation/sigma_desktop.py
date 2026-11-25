"""Narrow UIA boundary for an independently opened, approved desktop 1120 draft.

Profiles are supplied by the firm; there are no built-in Sigma selectors. This
supports only synchronous, profile-proven Value and Invoke patterns. Launch,
login, lookup, creation, append and independent reopen require separate licensed
proofs. A native save acknowledgement is not verification after reopen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import file_digest
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import sys
from threading import get_ident
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import AfterValidator, Field, StrictInt, StringConstraints, model_validator

from .contracts import Contract
from .native_plan import DraftIdentity, FieldPlan, RecordPlan
from .tool_contract import Ref


class DesktopBlocked(RuntimeError):
    """No action was dispatched; the named precondition requires attention."""


class DesktopUncertain(DesktopBlocked):
    """An action was dispatched; stop and reconcile before any additional operation."""


_Label = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
_ControlType = Literal[
    "Window", "Pane", "Group", "Custom", "Tab", "TabItem", "Text", "Edit", "Button",
    "List", "ListItem", "Table", "DataGrid", "DataItem",
]
_CONTAINERS = {"Pane", "Group", "Custom", "Tab", "TabItem", "List", "ListItem", "Table", "DataGrid", "DataItem"}


class DesktopControl(Contract):
    automation_id: _Label
    control_type: _ControlType


def _ending_in(*types: _ControlType):
    def check(path: tuple[DesktopControl, ...]) -> tuple[DesktopControl, ...]:
        if path[-1].control_type not in types or any(item.control_type not in _CONTAINERS for item in path[:-1]):
            raise ValueError("Invalid scoped UIA control path.")
        return path
    return check


_ControlPath = Annotated[tuple[DesktopControl, ...], Field(min_length=1, max_length=6)]
_TextPath = Annotated[_ControlPath, AfterValidator(_ending_in("Text"))]
_EditPath = Annotated[_ControlPath, AfterValidator(_ending_in("Edit"))]
_ReadPath = Annotated[_ControlPath, AfterValidator(_ending_in("Edit", "Text"))]
_ButtonPath = Annotated[_ControlPath, AfterValidator(_ending_in("Button"))]
_ContainerPath = Annotated[_ControlPath, AfterValidator(_ending_in(*_CONTAINERS))]


def _exe_path(value: str) -> str:
    path = PureWindowsPath(value)
    if (not path.is_absolute() or not re.fullmatch(r"[a-zA-Z]:", path.drive) or str(path) != value
            or path.suffix.lower() != ".exe" or any(
                part == ".." or part.endswith((".", " ")) or re.search(r'[\x00-\x1f<>:"|?*]', part)
                for part in path.parts[1:]
            )):
        raise ValueError("An exact absolute Windows executable path on a local drive is required.")
    return value


def _distinct(*items: Any) -> None:
    if len(set(items)) != len(items):
        raise ValueError("Profile roles must refer to distinct controls or semantic keys.")


class DesktopIdentity(Contract):
    draft_key: _TextPath
    client_reference: _TextPath
    tax_year: _TextPath
    return_type: _TextPath
    filing_status: _TextPath
    form_1120_value: _Label
    unfiled_value: _Label

    @model_validator(mode="after")
    def separate_identity_fields(self):
        _distinct(self.draft_key, self.client_reference, self.tax_year, self.return_type, self.filing_status)
        return self


class DesktopField(Contract):
    key: Ref
    edit: _EditPath


class DesktopRecordIdentity(Contract):
    key: Ref
    value: _TextPath


class DesktopRecords(Contract):
    container: _ContainerPath
    count: _TextPath  # from the section, independent of the realized UIA children
    row: DesktopControl  # immediate children of the container
    external_key: _TextPath  # relative to a row
    identity: Annotated[tuple[DesktopRecordIdentity, ...], Field(min_length=1, max_length=10)]
    max_rows: Annotated[StrictInt, Field(ge=1, le=256)]

    @model_validator(mode="after")
    def separate_row_roles(self):
        if self.row.control_type not in {"ListItem", "DataItem"}:
            raise ValueError("Record membership requires an exact list or data item type.")
        _distinct(*(item.key for item in self.identity))
        _distinct(self.external_key, *(item.value for item in self.identity))
        return self


class DesktopDetailField(DesktopField):
    parent_readback: _ReadPath  # relative to the opening row or section


class DesktopDetail(Contract):
    window: DesktopControl
    marker: _TextPath  # relative to the child modal
    marker_value: _Label
    open_button: _ButtonPath  # relative to the opening row or section
    commit_button: _ButtonPath  # relative to the child modal
    fields: Annotated[tuple[DesktopDetailField, ...], Field(min_length=1, max_length=32)]
    record_key: _TextPath | None = None  # mandatory in a row-specific modal

    @model_validator(mode="after")
    def separate_modal_roles(self):
        if self.window.control_type != "Window" or self.marker == self.record_key:
            raise ValueError("Detail requires a dedicated native modal and distinct context markers.")
        _distinct(*(item.key for item in self.fields))
        _distinct(*(item.edit for item in self.fields))
        _distinct(*(item.parent_readback for item in self.fields))
        return self


class DesktopSave(Contract):
    button: _ButtonPath
    state: _TextPath
    dirty_value: _Label
    saved_value: _Label

    @model_validator(mode="after")
    def separate_states(self):
        _distinct(self.dirty_value, self.saved_value)
        return self


class DesktopProfile(Contract):
    profile_id: Ref
    version: Annotated[StrictInt, Field(ge=1)]
    approval_ref: Ref  # from trusted deployment configuration, not from a tool request
    executable: Annotated[_Label, AfterValidator(_exe_path)]
    executable_sha256: Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
    tax_year: Annotated[StrictInt, Field(ge=1900, le=2200)]
    window: DesktopControl
    identity: DesktopIdentity
    section_key: Ref
    section: _ContainerPath
    section_marker: _TextPath
    section_value: _Label
    save: DesktopSave
    fields: Annotated[tuple[DesktopField, ...], Field(max_length=32)] = ()
    records: DesktopRecords | None = None
    detail: DesktopDetail | None = None

    @model_validator(mode="after")
    def separate_section_roles(self):
        if self.window.control_type != "Window":
            raise ValueError("The main control must be an exact top-level window.")
        _distinct(*(item.key for item in self.fields))
        _distinct(*(item.edit for item in self.fields))
        _distinct(self.section_marker, self.save.state, *((self.records.count,) if self.records else ()))
        if self.detail is not None:
            _distinct(self.window, self.detail.window)
            if self.detail.record_key is not None and self.records is None:
                raise ValueError("Row detail requires a complete profiled record list.")
            if self.detail.record_key is None:
                _distinct(self.save.button, self.detail.open_button)
        return self


@dataclass(frozen=True, slots=True)
class DesktopObservation:
    reference: str
    section_key: str
    detail_open: bool
    record_key: str | None


@dataclass(frozen=True, slots=True)
class DesktopRecord:
    external_key: str
    identity: tuple[tuple[str, str], ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Window:
    handle: int
    pid: int
    owner: int
    visible_root: bool
    enabled: bool


@dataclass(frozen=True, slots=True)
class _Process:
    executable: str
    created_ns: int


@dataclass(frozen=True, slots=True)
class _Element:
    selector: tuple[str, str]
    pid: int
    handle: int
    runtime_id: tuple[int, ...]
    visible: bool
    enabled: bool
    password: bool


@dataclass(slots=True)
class _View:
    foreground: _Window
    section: Any
    modal: Any | None


@dataclass(slots=True)
class _OpenDetail:
    handle: int
    runtime_id: tuple[int, ...]
    record: DesktopRecord | None
    field_plans: tuple[FieldPlan, ...]
    baseline: tuple[str, ...]
    values: tuple[str, ...]


def _file_stamp(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, getattr(info, "st_birthtime_ns", info.st_ctime_ns)


class _WindowsUIA:
    """Windows calls are loaded only by an explicit attachment, on its owning thread."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise DesktopBlocked("Desktop UIA requires an authorized interactive Windows session.")

        from ctypes import byref, create_unicode_buffer, WinDLL
        from ctypes.wintypes import BOOL, DWORD, HANDLE, LPDWORD, LPWSTR

        import win32api
        import win32con
        import win32file
        import win32gui
        import win32process
        import win32service
        import win32ts
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        query = WinDLL("kernel32", use_last_error=True).QueryFullProcessImageNameW
        query.argtypes = HANDLE, DWORD, LPWSTR, LPDWORD
        query.restype = BOOL

        def image(handle: int) -> str:
            buf = create_unicode_buffer(32768)
            size = DWORD(len(buf))
            if not query(handle, 0, buf, byref(size)):
                raise DesktopBlocked("Cannot verify the native process executable path.")
            return buf.value

        self.api, self.constants, self.files, self.gui = win32api, win32con, win32file, win32gui
        self.processes, self.services, self.sessions = win32process, win32service, win32ts
        self._image = image
        self._uia, self._info, self._wrapper = IUIA(), UIAElementInfo, UIAWrapper
        self._conditions = {}

    def process(self, pid: int) -> _Process:
        # QueryFullProcessImageName works across the worker/target bitness boundary.
        handle = self.api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            created = self.processes.GetProcessTimes(handle)["CreationTime"]
            return _Process(self._image(int(handle)), round(created.timestamp() * 1_000_000_000))
        finally:
            handle.Close()

    def build_stamp(self, executable: str) -> tuple[int, int, int, int, int]:
        path = Path(executable)
        if (self.files.GetDriveType(path.anchor) != self.constants.DRIVE_FIXED
                or ntpath.normcase(str(path.resolve(strict=True))) != ntpath.normcase(executable)):
            raise DesktopBlocked("The approved build must be installed at its exact local path.")
        return _file_stamp(path.stat())

    def approve_build(self, profile: DesktopProfile, process: _Process) -> tuple[int, int, int, int, int]:
        executable = profile.executable
        stamp = self.build_stamp(executable)
        with open(executable, "rb") as image:
            if max(stamp[-2:]) > process.created_ns or _file_stamp(os.fstat(image.fileno())) != stamp:
                raise DesktopBlocked("Restart the approved application from the unchanged installed build.")
            if file_digest(image, "sha256").hexdigest() != profile.executable_sha256:
                raise DesktopBlocked("The native executable does not match the approved build hash.")
        if self.build_stamp(executable) != stamp:
            raise DesktopBlocked("The approved build changed during verification.")
        return stamp

    def windows(self, main: int) -> tuple[_Window, _Window]:
        api, con, gui, service, wts = self.api, self.constants, self.gui, self.services, self.sessions
        sid = wts.ProcessIdToSessionId(os.getpid())
        if not sid or wts.WTSQuerySessionInformation(wts.WTS_CURRENT_SERVER_HANDLE, sid, wts.WTSConnectState) != wts.WTSActive:
            raise DesktopBlocked("Connect and unlock the authorized Windows session.")

        station = service.GetProcessWindowStation()
        thread_desktop = service.GetThreadDesktop(api.GetCurrentThreadId())
        try:
            if (service.GetUserObjectInformation(station, service.UOI_NAME) != "WinSta0"
                    or service.GetUserObjectInformation(thread_desktop, service.UOI_NAME) != "Default"):
                raise DesktopBlocked("An interactive Windows desktop is required.")
        finally:
            station.Detach()  # Borrowed handles; Windows owns them.
            thread_desktop.Detach()
        input_desktop = service.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        try:
            if service.GetUserObjectInformation(input_desktop, service.UOI_NAME) != "Default":
                raise DesktopBlocked("Unlock the authorized Windows desktop.")
        finally:
            input_desktop.CloseDesktop()

        def window(hwnd: int) -> _Window:
            if not hwnd or not gui.IsWindow(hwnd):
                raise DesktopBlocked("The authorized native window is unavailable.")
            _, pid = self.processes.GetWindowThreadProcessId(hwnd)
            visible = (gui.GetAncestor(hwnd, con.GA_ROOT) == hwnd and gui.IsWindowVisible(hwnd)
                       and not gui.IsIconic(hwnd) and not gui.IsHungAppWindow(hwnd))
            if not visible or wts.ProcessIdToSessionId(pid) != sid:
                raise DesktopBlocked("The foreground window is not available in the authorized Windows session.")
            return _Window(hwnd, pid, gui.GetWindow(hwnd, con.GW_OWNER), bool(visible), bool(gui.IsWindowEnabled(hwnd)))

        anchor = window(main)
        foreground = gui.GetForegroundWindow()
        return anchor, anchor if foreground == main else window(foreground)

    def root(self, hwnd: int) -> Any:
        return self._info(hwnd).element

    def inspect(self, node: Any) -> _Element:
        return _Element(
            (node.CurrentAutomationId, self._uia.known_control_type_ids[node.CurrentControlType]),
            node.CurrentProcessId, node.CurrentNativeWindowHandle,
            tuple(node.GetRuntimeId() or ()), not node.CurrentIsOffscreen,
            bool(node.CurrentIsEnabled), bool(node.CurrentIsPassword),
        )

    def children(self, scope: Any, control: DesktopControl, limit: int) -> tuple[Any, ...]:
        # Query only the exact next hop; don't scan other top-level windows, other
        # branches, or a global tree. Do not discard failed matches (ambiguity).
        uia = self._uia
        key = control.automation_id, control.control_type
        if key not in self._conditions:
            self._conditions[key] = uia.iuia.CreateAndCondition(
                uia.iuia.CreatePropertyCondition(uia.UIA_dll.UIA_AutomationIdPropertyId, control.automation_id),
                uia.iuia.CreatePropertyCondition(uia.UIA_dll.UIA_ControlTypePropertyId,
                                                 uia.known_control_types[control.control_type]),
            )
        results = scope.FindAll(uia.tree_scope["children"], self._conditions[key])
        count = results.Length
        if count > limit:
            raise DesktopBlocked("The profiled UIA control or record list is ambiguous or exceeds its bound.")
        return tuple(results.GetElement(i) for i in range(count))

    def read(self, node: Any, kind: _ControlType) -> str:
        if kind == "Text":
            return node.CurrentName
        return self._wrapper(self._info(node)).iface_value.CurrentValue

    def value_setter(self, node: Any) -> Callable[[str], None]:
        pattern = self._wrapper(self._info(node)).iface_value
        if pattern.CurrentIsReadOnly:
            raise DesktopBlocked("The exact native field is read-only.")
        return pattern.SetValue

    def button_invoker(self, node: Any) -> Callable[[], None]:
        return self._wrapper(self._info(node)).iface_invoke.Invoke

    def is_modal(self, node: Any) -> bool:
        return bool(self._wrapper(self._info(node)).iface_window.CurrentIsModal)


class DesktopSession:
    """Local operations under an existing exclusive owner and OS-held session lock.

    The trusted caller supplies an already verified assignment and journals write
    intent before calling mutation methods. Neither model tools nor an untrusted
    configuration may call this boundary directly. An uncertain session is spent.
    """

    @classmethod
    def attach(
        cls, profile: DesktopProfile, identity: DraftIdentity, draft_key: str, main_window: int, *,
        verify_owner: Callable[[DesktopProfile, DraftIdentity, str], bool],
    ) -> DesktopSession:
        if (identity.return_type != "1120" or identity.tax_year != profile.tax_year
                or not isinstance(draft_key, str) or not 0 < len(draft_key) <= 500 or not draft_key.strip()
                or type(main_window) is not int or main_window <= 0):
            raise DesktopBlocked("An exact unfiled 1120 assignment for this approved year is required.")
        try:
            if verify_owner(profile, identity, draft_key) is not True:
                raise DesktopBlocked("The desktop return has no verified exclusive owner.")
            backend = _WindowsUIA()
            anchor, foreground = backend.windows(main_window)
            if anchor.handle != main_window or anchor != foreground or not anchor.visible_root or not anchor.enabled or anchor.owner:
                raise DesktopBlocked("Bring the independently opened authorized draft to the foreground.")
            process = backend.process(anchor.pid)
            if ntpath.normcase(process.executable) != ntpath.normcase(profile.executable):
                raise DesktopBlocked("The native window does not belong to the approved executable.")
            stamp = backend.approve_build(profile, process)
            session = cls(profile, identity, draft_key, anchor, process, stamp, backend, verify_owner)
            view = session._view()
            if session._save_state(view) != profile.save.saved_value:
                raise DesktopBlocked("Clear or save existing native edits before handing off the draft.")
            return session
        except DesktopBlocked:
            raise
        except Exception:
            raise DesktopBlocked("Cannot attest the approved desktop build, session, or draft controls.") from None

    def __init__(self, profile, identity, draft_key, anchor, process, stamp, backend, verify_owner) -> None:
        self._profile, self._identity, self._draft_key = profile, identity, draft_key
        self._anchor, self._process, self._stamp, self._backend = anchor, process, stamp, backend
        self._verify_owner = verify_owner
        self._thread = get_ident()
        self._root_id: tuple[int, ...] | None = None
        self._section_id: tuple[int, ...] | None = None
        self._detail: _OpenDetail | None = None
        self._observation: DesktopObservation | None = None
        self._uncertain = self._dirty = False

    def _frames(self) -> tuple[_Window, _Window]:
        if self._uncertain:
            raise DesktopUncertain("Stop and reconcile the native draft before any additional desktop work.")
        if get_ident() != self._thread:
            raise DesktopBlocked("Use the authorized desktop attachment on its owning thread.")
        if self._verify_owner(self._profile, self._identity, self._draft_key) is not True:
            raise DesktopBlocked("The desktop return's exclusive owner was lost.")
        main, active = self._backend.windows(self._anchor.handle)
        if (main.handle != self._anchor.handle or main.pid != self._anchor.pid or main.owner or not main.visible_root
                or self._backend.process(main.pid) != self._process
                or self._backend.build_stamp(self._profile.executable) != self._stamp):
            raise DesktopBlocked("The approved process, build, or main window changed.")
        if active == main:
            if not main.enabled:
                raise DesktopBlocked("The approved draft is blocked by another native dialog.")
        elif (active.pid != main.pid or active.owner != main.handle or main.enabled
              or not active.visible_root or not active.enabled):
            raise DesktopBlocked("An unapproved window or dialog has the foreground.")
        return main, active

    def _verified(self, node: Any, selector: DesktopControl, *, action: bool = False) -> _Element:
        state = self._backend.inspect(node)
        if (state.pid != self._anchor.pid or state.selector != (selector.automation_id, selector.control_type)
                or not state.visible or state.password or not state.runtime_id or (action and not state.enabled)):
            raise DesktopBlocked("The exact UIA control is unavailable or outside the approved process.")
        return state

    def _control(self, scope: Any, path: _ControlPath, *, action: bool = False) -> Any:
        for index, selector in enumerate(path):
            found = self._backend.children(scope, selector, 1)
            if len(found) != 1:
                raise DesktopBlocked("A required scoped control is missing or ambiguous.")
            scope = found[0]
            self._verified(scope, selector, action=action and index == len(path) - 1)
        return scope

    def _read(self, scope: Any, path: _ControlPath) -> str:
        control = self._control(scope, path)
        text = self._backend.read(control, path[-1].control_type)
        if not isinstance(text, str) or len(text) > 2048 or "\0" in text:
            raise DesktopBlocked("Cannot attest the native control's text value.")
        return text

    def _view(self, *, opening: bool = False, closing: bool = False) -> _View:
        try:
            frames = self._frames()
            main, active = frames
            profile = self._profile
            modal = None
            if active != main:
                detail = profile.detail
                if (detail is None or closing or (not opening and (
                        self._detail is None or active.handle != self._detail.handle))):
                    raise DesktopBlocked("An unapproved native dialog has the foreground.")
                modal = self._backend.root(active.handle)
                state = self._verified(modal, detail.window, action=True)
                if (state.handle != active.handle or not self._backend.is_modal(modal)
                        or (not opening and state.runtime_id != self._detail.runtime_id)
                        or self._read(modal, detail.marker) != detail.marker_value):
                    raise DesktopBlocked("Cannot verify the approved owned child dialog.")
            elif opening or (self._detail is not None and not closing):
                raise DesktopBlocked("The required owned child dialog is not in the foreground.")

            root = self._backend.root(main.handle)
            state = self._verified(root, profile.window)
            if state.handle != main.handle or (self._root_id is not None and state.runtime_id != self._root_id):
                raise DesktopBlocked("The authorized UIA main window changed.")
            identity = profile.identity
            if any(self._read(root, path) != value for path, value in (
                (identity.draft_key, self._draft_key), (identity.client_reference, self._identity.client_reference),
                (identity.tax_year, str(profile.tax_year)), (identity.return_type, identity.form_1120_value),
                (identity.filing_status, identity.unfiled_value),
            )):
                raise DesktopBlocked("The native draft, client, year, 1120 type, or unfiled status does not match.")
            section = self._control(root, profile.section)
            section_id = self._backend.inspect(section).runtime_id
            if ((self._section_id is not None and section_id != self._section_id)
                    or self._read(section, profile.section_marker) != profile.section_value):
                raise DesktopBlocked("The approved native draft section changed.")
            view = _View(active, section, modal)
            if modal is not None and self._detail is not None:
                self._verify_detail_record(view, self._detail.record)
            if self._frames() != frames:
                raise DesktopBlocked("The authorized foreground window changed during observation.")
            self._root_id, self._section_id = state.runtime_id, section_id
            return view
        except DesktopBlocked:
            raise
        except Exception:
            raise DesktopBlocked("Cannot read the exact authorized native UIA context.") from None

    def _verify_detail_record(self, view: _View, record: DesktopRecord | None) -> None:
        if record is None:
            return
        detail = self._profile.detail
        if self._read(view.modal, detail.record_key) != record.external_key:
            raise DesktopBlocked("The child dialog does not identify the same native record.")
        self._record_scope(view, record)

    def _records(self, view: _View) -> tuple[tuple[DesktopRecord, Any], ...]:
        profile = self._profile.records
        if profile is None:
            raise DesktopBlocked("No complete repeating-record list has been approved for this section.")
        count = self._read(view.section, profile.count)
        if not re.fullmatch(r"0|[1-9][0-9]{0,2}", count) or int(count) > profile.max_rows:
            raise DesktopBlocked("Cannot attest a bounded native record count.")
        scope = self._control(view.section, profile.container)
        nodes = self._backend.children(scope, profile.row, profile.max_rows)
        if len(nodes) != int(count):
            raise DesktopBlocked("The native record list is incomplete or has ambiguous membership.")
        records = []
        keys = set()
        for node in nodes:
            self._verified(node, profile.row)
            key = self._read(node, profile.external_key)
            identity = tuple((item.key, self._read(node, item.value)) for item in profile.identity)
            if (not key.strip() or len(key) > 120 or key in keys or any(not value.strip() for _, value in identity)):
                raise DesktopBlocked("Native records lack distinct durable keys or complete business identity.")
            keys.add(key)
            records.append((DesktopRecord(key, identity), node))
        if self._read(view.section, profile.count) != count:
            raise DesktopBlocked("The native record count changed during observation.")
        return tuple(records)

    def _record_scope(self, view: _View, record: DesktopRecord | None) -> Any:
        if record is None:
            return view.section
        for current, node in self._records(view):
            if current == record:
                return node
        raise DesktopBlocked("The same native record and business identity no longer match.")

    def _save_state(self, view: _View) -> str:
        return self._read(view.section, self._profile.save.state)

    def _fields(self, view: _View):
        return self._profile.fields if view.modal is None else self._profile.detail.fields

    def _field(self, view: _View, key: str) -> DesktopField:
        for item in self._fields(view):
            if item.key == key:
                return item
        raise DesktopBlocked("The field is outside the active profiled native context.")

    def observe(self) -> DesktopObservation:
        view = self._view()
        record = None if self._detail is None else self._detail.record
        self._observation = DesktopObservation(
            uuid4().hex, self._profile.section_key, view.modal is not None, None if record is None else record.external_key,
        )
        return self._observation

    def _observed(self, observation: DesktopObservation) -> _View:
        if observation is None or observation != self._observation:
            raise DesktopBlocked("Refresh the desktop observation; its reference is stale or belongs to another attachment.")
        return self._view()

    def read_field(self, observation: DesktopObservation, key: str) -> str:
        view = self._observed(observation)
        return self._read(self._active(view), self._field(view, key).edit)

    def list_records(self, observation: DesktopObservation) -> tuple[DesktopRecord, ...]:
        view = self._observed(observation)
        if view.modal is not None:
            raise DesktopBlocked("The record list is not active while a detail dialog is open.")
        return tuple(record for record, _ in self._records(view))

    @staticmethod
    def _active(view: _View) -> Any:
        return view.section if view.modal is None else view.modal

    def _action(self, view: _View, scope: Any, path: _ControlPath, *, value: str | None = None) -> Callable[[], None]:
        try:
            control = self._control(scope, path, action=True)
            before = self._verified(control, path[-1], action=True)
            if value is None:
                run = self._backend.button_invoker(control)
            else:
                setter = self._backend.value_setter(control)
                run = lambda: setter(value)
            if self._verified(control, path[-1], action=True) != before or self._frames()[1] != view.foreground:
                raise DesktopBlocked("The approved control or foreground changed before dispatch.")
            return run
        except DesktopBlocked:
            raise
        except Exception:
            raise DesktopBlocked("The profiled native control does not expose the required UIA pattern.") from None

    def _dispatch(self, run: Callable[[], None], readback: Callable[[], None]) -> None:
        self._observation = None
        try:
            run()
            readback()
        except Exception:
            self._uncertain = True
            raise DesktopUncertain("The desktop action was dispatched but readback failed; reconcile the exact draft before retrying.") from None

    def set_field(self, observation: DesktopObservation, plan: FieldPlan) -> Literal["applied", "unchanged"]:
        view = self._observed(observation)
        if (self._detail is not None and self._detail.record is not None
                and plan not in self._detail.field_plans):
            raise DesktopBlocked("The field binding does not belong to the active approved record.")
        item = self._field(view, plan.field_key)
        active = self._active(view)
        current = self._read(active, item.edit)
        if plan.matches(current):
            return "unchanged"
        if not plan.may_enter(current):
            raise DesktopBlocked("The saved baseline conflicts with the planned native value or belongs to the preparer.")
        if "\0" in plan.value:
            raise DesktopBlocked("The planned native text is not a valid UIA value.")
        previous = None if self._detail is None else self._detail.values
        if self._detail is not None and self._detail_values(view) != previous:
            raise DesktopBlocked("The open detail contains an unexplained edit.")

        def readback() -> None:
            after = self._view()
            actual = self._read(self._active(after), item.edit)
            if not plan.matches(actual):
                raise DesktopBlocked("The native field did not accept the approved value.")
            if self._detail is None:
                self._dirty = True
            else:
                expected = tuple(actual if field.key == item.key else old
                                 for field, old in zip(self._profile.detail.fields, previous, strict=True))
                if self._detail_values(after) != expected:
                    raise DesktopBlocked("A different value in the detail changed unexpectedly.")
                self._detail.values = expected

        self._dispatch(self._action(view, active, item.edit, value=plan.value), readback)
        return "applied"

    def _detail_values(self, view: _View) -> tuple[str, ...]:
        return tuple(self._read(view.modal, item.edit) for item in self._profile.detail.fields)

    def _parent_values(self, scope: Any) -> tuple[str, ...]:
        return tuple(self._read(scope, item.parent_readback) for item in self._profile.detail.fields)

    def open_detail(self, observation: DesktopObservation, record: RecordPlan | None = None) -> None:
        view = self._observed(observation)
        detail = self._profile.detail
        if view.modal is not None or detail is None or (record is None) != (detail.record_key is None):
            raise DesktopBlocked("The authorized detail requires its exact parent section or record context.")
        row = None
        scope = view.section
        if record is not None:
            matches = [(item, node) for item, node in self._records(view) if dict(item.identity) == record.identity]
            if len(matches) != 1 or (record.external_key is not None and matches[0][0].external_key != record.external_key):
                raise DesktopBlocked("The approved native record is missing or ambiguous; row creation is not enabled.")
            row, scope = matches[0]
            allowed_fields = {item.key for item in detail.fields}
            if any(binding.field_key not in allowed_fields for binding in record.fields):
                raise DesktopBlocked("The approved record has a binding outside its native detail.")
        field_plans = tuple(binding.model_copy(deep=True) for binding in record.fields) if record is not None else ()
        parent_values = self._parent_values(scope)

        def readback() -> None:
            after = self._view(opening=True)
            self._verify_detail_record(after, row)
            if self._detail_values(after) != parent_values:
                raise DesktopBlocked("The owned child does not match its parent's visible values.")
            self._detail = _OpenDetail(after.foreground.handle, self._backend.inspect(after.modal).runtime_id,
                                       row, field_plans, parent_values, parent_values)

        self._dispatch(self._action(view, scope, detail.open_button), readback)

    def commit_detail(self, observation: DesktopObservation) -> None:
        view = self._observed(observation)
        detail = self._detail
        if detail is None or detail.values == detail.baseline or self._detail_values(view) != detail.values:
            raise DesktopBlocked("There are no verified pending edits in the owned detail.")
        if self._parent_values(self._record_scope(view, detail.record)) != detail.baseline:
            raise DesktopBlocked("The parent values changed before the child commit.")

        def readback() -> None:
            after = self._view(closing=True)
            if self._parent_values(self._record_scope(after, detail.record)) != detail.values:
                raise DesktopBlocked("The parent did not acknowledge the exact child values and record.")
            self._detail = None
            self._dirty = True

        self._dispatch(self._action(view, view.modal, self._profile.detail.commit_button), readback)

    def _save_contents(
        self, view: _View,
    ) -> tuple[tuple[str, ...], tuple[str, ...], dict[DesktopRecord, tuple[str, ...]]]:
        detail = self._profile.detail
        row_detail = detail is not None and detail.record_key is not None
        fields = tuple(self._read(view.section, item.edit) for item in self._profile.fields)
        section_detail = self._parent_values(view.section) if detail is not None and not row_detail else ()
        records: dict[DesktopRecord, tuple[str, ...]] = {}
        if self._profile.records is not None:
            for record, node in self._records(view):
                records[record] = self._parent_values(node) if row_detail else ()
        return fields, section_detail, records

    def save_draft(self, observation: DesktopObservation) -> Literal["acknowledged"]:
        view = self._observed(observation)
        if view.modal is not None or not self._dirty or self._save_state(view) != self._profile.save.dirty_value:
            raise DesktopBlocked("Draft Save requires the verified parent and its approved dirty state.")
        contents = self._save_contents(view)

        def readback() -> None:
            after = self._view()
            if self._save_state(after) != self._profile.save.saved_value or self._save_contents(after) != contents:
                raise DesktopBlocked("The same draft did not acknowledge Save with unchanged observed values and records.")
            self._dirty = False

        self._dispatch(self._action(view, view.section, self._profile.save.button), readback)
        return "acknowledged"
