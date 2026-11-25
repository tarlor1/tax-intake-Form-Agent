"""Drive the real desktop controller against a synthetic 1120 control tree.

Only the operating-system/UIA boundary is simulated. This test can run on Linux.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from src.preparation import sigma_desktop as desktop
from src.preparation.native_plan import DraftIdentity, FieldPlan, RecordPlan


IDENTITY = DraftIdentity(client_reference="TEST-CORP-01", tax_year=2025, return_type="1120")
DRAFT = "synthetic-1120"
EXE = r"C:\Practice\Ledger1120.exe"


def path(identifier, kind="Text"):
    return ({"automation_id": identifier, "control_type": kind},)


def profile():
    return desktop.DesktopProfile.model_validate({
        "profile_id": "test-1120", "version": 1, "approval_ref": "synthetic-only",
        "executable": EXE, "executable_sha256": "a" * 64, "tax_year": IDENTITY.tax_year,
        "window": path("draft", "Window")[0],
        "identity": {
            **{key: path(key) for key in ("draft_key", "client_reference", "tax_year", "return_type", "filing_status")},
            "form_1120_value": "1120", "unfiled_value": "Unfiled",
        },
        "section_key": "deductions", "section": path("deductions", "Pane"),
        "section_marker": path("section"), "section_value": "Other deductions",
        "save": {"button": path("save", "Button"), "state": path("state"), "dirty_value": "Dirty", "saved_value": "Saved"},
        "records": {
            "container": path("expenses", "List"), "count": path("count"), "row": path("expense", "ListItem")[0],
            "external_key": path("record"), "identity": ({"key": "ledger-reference", "value": path("reference")},),
            "max_rows": 10,
        },
        "detail": {
            "window": path("expense-editor", "Window")[0], "marker": path("title"), "marker_value": "Edit expense",
            "open_button": path("edit", "Button"), "commit_button": path("commit", "Button"),
            "record_key": path("record"),
            "fields": ({"key": "amount", "edit": path("amount", "Edit"), "parent_readback": path("amount")},),
        },
    })


@dataclass
class Control:
    name: str
    kind: str = "Text"
    value: str = ""
    handle: int = 0
    invoke: Callable[[], None] | None = None
    children: list["Control"] = field(default_factory=list)


class SyntheticUIA:
    pid = 42
    main_handle = 1
    modal_handle = 2
    stamp = (1, 2, 3, 4, 5)

    def __init__(self, reverted: bool):
        self.modal: Control | None = None
        self.reverted = reverted
        self.save_state = Control("state", value="Saved")
        self.amounts = {key: Control("amount") for key in ("row-1", "row-2")}
        self.saved: dict[str, str] = {}
        rows = [self._row(key, reference) for key, reference in (("row-1", "D-101"), ("row-2", "D-222"))]
        section = Control("deductions", "Pane", children=[
            Control("section", value="Other deductions"), Control("count", value=str(len(rows))),
            self.save_state, Control("save", "Button", invoke=self._save), Control("expenses", "List", children=rows),
        ])
        self.main = Control("draft", "Window", handle=self.main_handle, children=[
            Control("draft_key", value=DRAFT), Control("client_reference", value=IDENTITY.client_reference),
            Control("tax_year", value=str(IDENTITY.tax_year)), Control("return_type", value="1120"),
            Control("filing_status", value="Unfiled"), section,
        ])

    def _row(self, key, reference):
        return Control("expense", "ListItem", children=[
            Control("record", value=key), Control("reference", value=reference), self.amounts[key],
            Control("edit", "Button", invoke=lambda: self._edit(key)),
        ])

    def _edit(self, key):
        amount = Control("amount", "Edit", self.amounts[key].value)

        def commit():
            self.amounts[key].value = amount.value
            self.modal = None
            self.save_state.value = "Dirty"

        self.modal = Control("expense-editor", "Window", handle=self.modal_handle, children=[
            Control("title", value="Edit expense"), Control("record", value=key), amount,
            Control("commit", "Button", invoke=commit),
        ])

    def _save(self):
        if self.reverted:
            self.amounts["row-1"].value = ""
        self.saved = {key: amount.value for key, amount in self.amounts.items()}
        self.save_state.value = "Saved"

    def windows(self, handle):
        assert handle == self.main_handle
        main = desktop._Window(handle, self.pid, 0, True, self.modal is None)
        foreground = main if self.modal is None else desktop._Window(self.modal_handle, self.pid, handle, True, True)
        return main, foreground

    def process(self, pid):
        assert pid == self.pid
        return desktop._Process(EXE, 6)

    def approve_build(self, approved, process):
        assert approved.executable == process.executable == EXE
        return self.stamp

    def build_stamp(self, executable):
        assert executable == EXE
        return self.stamp

    def root(self, handle):
        return self.main if handle == self.main_handle else self.modal

    def inspect(self, node):
        return desktop._Element((node.name, node.kind), self.pid, node.handle, (id(node),), True, True, False)

    @staticmethod
    def children(scope, selector, limit):
        children = tuple(node for node in scope.children if (node.name, node.kind) == (selector.automation_id, selector.control_type))
        if len(children) > limit:
            raise desktop.DesktopBlocked("Synthetic selector is ambiguous.")
        return children

    @staticmethod
    def read(node, kind):
        assert node.kind == kind
        return node.value

    @staticmethod
    def value_setter(node):
        return lambda value: setattr(node, "value", value)

    @staticmethod
    def button_invoker(node):
        assert node.invoke is not None
        return node.invoke

    def is_modal(self, node):
        return node is self.modal


@pytest.mark.parametrize("reverted", [False, True], ids=["saved-both-rows", "saved-label-but-missing-expense"])
def test_desktop_save_checks_each_committed_nested_expense(monkeypatch, reverted):
    target = SyntheticUIA(reverted)
    monkeypatch.setattr(desktop, "_WindowsUIA", lambda: target)
    approved = profile()
    session = desktop.DesktopSession.attach(
        approved, IDENTITY, DRAFT, target.main_handle,
        verify_owner=lambda selected, identity, key: (selected, identity, key) == (approved, IDENTITY, DRAFT),
    )
    records = session.list_records(session.observe())
    for record in records:
        field = FieldPlan(field_key="amount", value="125.00", kind="decimal", binding_id=record.external_key,
                          fact_ids=(f"fact-{record.external_key}",))
        plan = RecordPlan(source_record_id=record.external_key, identity=dict(record.identity),
                          fields=(field,), external_key=record.external_key)
        session.open_detail(session.observe(), plan)
        observation = session.observe()
        assert observation.record_key == record.external_key and session.read_field(observation, "amount") == ""
        other_key = next(item.external_key for item in records if item.external_key != record.external_key)
        for foreign_binding in (
            field.model_copy(update={"binding_id": other_key, "fact_ids": (f"fact-{other_key}",)}),
            field.model_copy(update={"value": "999.00"}),
        ):
            with pytest.raises(desktop.DesktopBlocked, match="does not belong to the active approved record"):
                session.set_field(observation, foreign_binding)
            assert session.read_field(observation, "amount") == ""
        assert session.set_field(observation, field) == "applied"
        session.commit_detail(session.observe())

    if reverted:
        with pytest.raises(desktop.DesktopUncertain, match="readback failed"):
            session.save_draft(session.observe())
        assert target.saved == {"row-1": "", "row-2": "125.00"}
        with pytest.raises(desktop.DesktopUncertain, match="reconcile"):
            session.observe()
    else:
        assert session.save_draft(session.observe()) == "acknowledged"
        assert target.saved == {"row-1": "125.00", "row-2": "125.00"}
