"""Metadata and canonical values for the unchanged external client intake form."""

from __future__ import annotations

import json
import re
from datetime import date

from .models import (
    DEDUCTIONS,
    FILING_STATUSES,
    FORM_FIELDS,
    INCOME_SOURCES,
    REQUIRED_FIELDS,
    US_STATES,
)

CONTRACT_VERSION = 1


def field_definitions() -> list[dict]:
    """Return fresh metadata in native form order, without manufacturing answers."""
    choices = {
        "marital_status": {
            value: value.title()
            for value in ("single", "married", "divorced", "widowed", "separated")
        },
        "preferred_contact_method": {"email": "Email", "phone": "Phone"},
        "state": US_STATES,
        "drivers_license_state": US_STATES,
        "filing_status": FILING_STATUSES,
        "income_sources": INCOME_SOURCES,
        "deductions": DEDUCTIONS,
        "urgency": {"low": "Low", "medium": "Medium", "high": "High"},
        "irs_notice_received": {"yes": "Yes", "no": "No"},
        "consultation_requested": {"yes": "Yes", "no": "No"},
    }
    sections = {
        "Taxpayer": (
            "full_name",
            "email",
            "phone",
            "date_of_birth",
            "marital_status",
            "preferred_contact_method",
        ),
        "Home address": ("street_address", "city", "state", "zip_code"),
        "Photo ID": (
            "drivers_license_number",
            "drivers_license_state",
            "drivers_license_issued_on",
            "drivers_license_expires_on",
        ),
        "Tax situation": (
            "tax_year",
            "filing_status",
            "number_of_dependents",
            "occupation",
            "employer",
            "income_sources",
            "deductions",
        ),
        "Request": (
            "urgency",
            "deadline_date",
            "irs_notice_received",
            "consultation_requested",
            "request_summary",
            "notes",
        ),
    }
    dates = {
        "date_of_birth",
        "drivers_license_issued_on",
        "drivers_license_expires_on",
        "deadline_date",
    }

    definitions = []
    for section, names in sections.items():
        for name in names:
            kind = (
                "multi"
                if name in {"income_sources", "deductions"}
                else "select"
                if name in choices
                else "date"
                if name in dates
                else "integer"
                if name in {"tax_year", "number_of_dependents"}
                else "text"
            )

            control = {"multi": "multi-checkbox", "integer": "number"}.get(kind, kind)
            if name in {"email", "phone", "notes", "request_summary"}:
                control = {
                    "email": "email",
                    "phone": "tel",
                    "notes": "textarea",
                    "request_summary": "textarea",
                }[name]

            item = {
                "id": name,
                "label": FORM_FIELDS.get(name, name.title()),
                "section": section,
                "kind": kind,
                "controlType": control,
                "required": name in REQUIRED_FIELDS or name == "urgency",
                "options": [
                    {"value": value, "label": label}
                    for value, label in choices.get(name, {}).items()
                ],
                "dependencies": [],
                "derivation": None,
            }
            if kind == "date":
                item["min"] = "1900-01-01"
                if name in {"date_of_birth", "drivers_license_issued_on"}:
                    item["max"] = date.today().isoformat()
            if kind == "integer":
                item.update(
                    min=1990 if name == "tax_year" else 0,
                    max=date.today().year if name == "tax_year" else 20,
                    step=1,
                )
            if name == "zip_code":
                item["pattern"] = r"\d{5}(-\d{4})?"
            definitions.append(item)

    return definitions


def _option(field: dict, value: str) -> str:
    matches = {
        option["value"]
        for option in field["options"]
        if value.casefold() in {option["value"].casefold(), option["label"].casefold()}
    }
    if len(matches) != 1:
        raise ValueError(f"Choose an allowed option for {field['label']}.")
    return matches.pop()


def normalize_value(field: dict, value: object) -> str | None:
    """Validate native scalar/choice values; missing stays missing. Raises ValueError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None

    kind = field["kind"]
    if kind == "multi":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError) as error:
                raise ValueError("Multiple choices must be a JSON array.") from error
        if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
            raise ValueError("Multiple choices must be an array of allowed strings.")
        selected = {_option(field, entry.strip()) for entry in value}
        ordered = [option["value"] for option in field["options"] if option["value"] in selected]
        return json.dumps(ordered, separators=(",", ":"))

    if not isinstance(value, str):
        raise ValueError("Intake scalar values must be strings.")
    value = value.strip()
    if len(value) > 10_000:
        raise ValueError("Intake values must be at most 10000 characters.")

    if kind == "select":
        return _option(field, value)

    if kind == "integer":
        if not re.fullmatch(r"\d+", value) or not field["min"] <= int(value) <= field["max"]:
            raise ValueError(
                f"{field['label']} must be an integer from {field['min']} through {field['max']}."
            )
        return str(int(value))

    if kind == "date":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError()
            date.fromisoformat(value)
            if value < field["min"] or (field.get("max") and value > field["max"]):
                raise ValueError()
        except ValueError as error:
            raise ValueError(
                f"{field['label']} must be a valid ISO date within the native form bounds."
            ) from error

    if field.get("controlType") == "email" and not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
        value,
    ):
        raise ValueError("Enter a valid email address.")
    if field.get("pattern") and not re.fullmatch(field["pattern"], value):
        raise ValueError(f"{field['label']} does not match the required format.")

    return value


def decode_value(field: dict, value: str | None) -> str | list[str]:
    """Decode a canonical value for normal native DOM controls."""
    normalized = normalize_value(field, value)
    if field["kind"] == "multi":
        return json.loads(normalized) if normalized is not None else []
    return normalized or ""
