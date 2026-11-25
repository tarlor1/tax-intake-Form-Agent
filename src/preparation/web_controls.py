"""Typed browser input operations on locators resolved by a trusted page adapter."""

from __future__ import annotations

from typing import Literal

from playwright.async_api import Locator

ControlKind = Literal["input", "select", "checkbox", "radio", "combobox"]


class ControlBlocked(RuntimeError):
    """The control or its requested choice is missing, ambiguous, or unsafe."""


async def _visible_enabled(control: Locator) -> None:
    if await control.count() != 1 or not await control.is_visible() or not await control.is_enabled():
        raise ControlBlocked("missing-or-ambiguous-form-control")


async def click_visible_target(control: Locator) -> None:
    """Click an exposed part of a unique control, never through an overlay."""
    await _visible_enabled(control)
    await control.scroll_into_view_if_needed()
    box = await control.bounding_box()
    if box is None:
        raise ControlBlocked("unreachable-control")
    for fraction in (0.5, 0.75, 0.25, 0.9, 0.1):
        point = {"x": box["width"] * fraction, "y": box["height"] / 2}
        receives_input = await control.evaluate(
            """(element, point) => {
                const rect = element.getBoundingClientRect();
                return element.contains(document.elementFromPoint(rect.left + point.x, rect.top + point.y));
            }""",
            point,
        )
        if receives_input:
            await control.click(position=point, timeout=5_000)
            return
    raise ControlBlocked("unreachable-control")


async def resolve_question_control(
    scope: Locator, group_selector: str, question: str, control_selector: str,
) -> Locator:
    """Resolve an approved control inside its exact question, not by answer text alone."""
    label = scope.page.get_by_text(question, exact=True).and_(scope.page.locator("label, legend, [role=alert]"))
    group = scope.locator(group_selector).filter(has=label)
    await _visible_enabled(group)
    control = group.locator(control_selector)
    await _visible_enabled(control)
    return control

async def read_control(control: Locator, kind: ControlKind) -> str:
    """Return the value or selected label used by the adapter's binding."""
    await _visible_enabled(control)
    if kind in {"checkbox", "radio"}:
        return "true" if await control.is_checked() else "false"
    if kind == "combobox":
        return (await control.inner_text()).strip()
    return await control.input_value()


async def enter_control(control: Locator, kind: ControlKind, value: str) -> None:
    """Perform a native input action; values must already be authorized by the caller."""
    await _visible_enabled(control)
    if kind == "select":
        matches = await control.locator("option").evaluate_all(
            "(options, value) => options.filter(option => !option.matches(':disabled') && option.value === value).length",
            value,
        )
        if matches != 1:
            raise ControlBlocked("missing-or-ambiguous-select-option")
        await control.select_option(value=value)
    elif kind in {"checkbox", "radio"}:
        if value not in {"true", "false"} or (kind == "radio" and value != "true"):
            raise ControlBlocked("invalid-toggle-value")
        await control.set_checked(value == "true")
    elif kind == "combobox":
        if await control.get_attribute("aria-expanded") != "false":
            raise ControlBlocked("unexpected-combobox-state")
        await control.press("Enter")
        popup_id = await control.get_attribute("aria-controls")
        if await control.get_attribute("aria-expanded") != "true" or not popup_id:
            raise ControlBlocked("unconfirmed-combobox-popup")
        popup = control.page.get_by_role("listbox").and_(control.page.locator("[id]").filter(
            has=control.page.get_by_role("option", name=value, exact=True),
        ))
        await _visible_enabled(popup)
        if await popup.get_attribute("id") != popup_id:
            raise ControlBlocked("unbound-combobox-popup")
        option = popup.get_by_role("option", name=value, exact=True)
        await click_visible_target(option)
        if await control.get_attribute("aria-expanded") != "false" or await read_control(control, kind) != value:
            raise ControlBlocked("unconfirmed-combobox-selection")
    else:
        if not await control.is_editable():
            raise ControlBlocked("form-control-not-editable")
        await control.fill(value)
