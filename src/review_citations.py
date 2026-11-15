"""Best-effort local locations for AI drafts, never extraction acceptance rules.

A match locates literal source text; it does not verify the draft's interpretation.
Unknown ownership and ambiguous matches deliberately remain document-context only.
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache

from .review_documents import DocumentError, literal_citation, source_pages


_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)
_CLIENT_HEADER = re.compile(
    r"^\s*(?:client|taxpayer|full (?:legal )?name|client name|entity|business)\s*:\s*(.+)", re.I
)
_YEAR = re.compile(r"\b(?:tax\s*year|taxes\s+for|return\s+for)\s*[:\-]?\s*(20\d{2}|19\d{2})\b", re.I)
_DATE_HINTS = {
    "date_of_birth": r"\b(?:born|birth|dob)\b",
    "drivers_license_issued_on": r"\b(?:issued|issue)\b",
    "drivers_license_expires_on": r"\b(?:expires?|expiration|expiry)\b",
    "deadline_date": r"\b(?:deadline|due)\b",
}
_SHARED_CLIENT_FIELDS = frozenset({
    "full_name", "email", "phone", "date_of_birth", "preferred_contact_method",
    "street_address", "city", "state", "zip_code",
})


@lru_cache(maxsize=8)
def citation_pages(content: bytes, media_type: str) -> list[dict]:
    """Reuse immutable source geometry across background presentation polls."""
    return source_pages(content, media_type)


def _space(value: str) -> str:
    return " ".join(value.split()).casefold()


def _contains(text: str, value: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(_space(value)) + r"(?!\w)", _space(text)))


def _date_matches(text: str, value: str) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    if _contains(text, value):
        return True
    month = _MONTHS[parsed.month - 1]
    month_pattern = rf"(?:{month}|{month[:3]}\.?)"
    day = rf"0?{parsed.day}(?:st|nd|rd|th)?"
    year = str(parsed.year)
    patterns = (
        rf"\b{month_pattern}\s+{day},?\s+{year}\b",
        rf"\b{day}\s+{month_pattern},?\s+{year}\b",
        rf"\b0?{parsed.month}[/-]0?{parsed.day}[/-]{year}\b",
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _matches(field: dict, value: str, text: str) -> bool:
    if field["kind"] == "date":
        hint = _DATE_HINTS.get(field["id"])
        return bool(hint and re.search(hint, text, re.I) and _date_matches(text, value))
    if field["kind"] == "multi":
        # A list can summarize several distant facts; don't invent one exact quote.
        return False
    if field["kind"] == "select":
        labels = [option["label"] for option in field.get("options", []) if option["value"] == value]
        aliases = [value.replace("_", " "), *labels]
        if field["id"] in {"state", "drivers_license_state"}:
            # Short state codes must not match ordinary words such as "or" or "in".
            hint = (
                r"\b(?:licen[cs]e|issuing|issued)\b"
                if field["id"] == "drivers_license_state"
                else r"\b(?:state|address|residen\w*|lives|mailing)\b"
            )
            if not re.search(hint, text, re.I):
                return False
            if field["id"] == "state" and re.search(r"\blicen[cs]e\b", text, re.I):
                return False
            return any(_contains(text, label) for label in labels) or bool(
                re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text)
            )
        hint = field["label"].split()[0]
        return bool(re.search(r"\b" + re.escape(hint) + r"\b", text, re.I)) and any(
            _contains(text, alias) for alias in aliases
        )
    if field["kind"] == "integer":
        hint = r"\b(?:tax\s*year|taxes\s+for|return\s+for)\b" if field["id"] == "tax_year" else r"\bdependents?\b"
        return bool(re.search(hint, text, re.I) and _contains(text, value))
    return _contains(text, value)


def match_citation(
    pages: list[dict], *, field: dict, value: str, media_type: str,
    client_name: str, tax_year: int, contexts: list[dict],
) -> dict | None:
    """Return one unambiguous, literal line location in this client's/year's scope.

    Known extraction groups identify prose section boundaries. Explicit client/year
    headings also protect legacy documents that predate stored group metadata.
    Shared or unclear multi-client/year prose is intentionally not auto-attributed.
    """
    target = _space(client_name)
    names = {_space(item["clientName"]) for item in contexts if item.get("clientName")}
    names.add(target)
    years = {item.get("taxYear") for item in contexts if _space(item.get("clientName") or "") == target}
    years.discard(None)
    years.add(tax_year)
    owner = None
    year = None
    matches = []
    for page in pages:
        for number, line in enumerate(page["lines"], 1):
            text = line["text"]
            header = _CLIENT_HEADER.match(text)
            mentioned = {name for name in names if name and _contains(text, name)}
            if header:
                named = {name for name in names if _contains(header.group(1), name)}
                owner = next(iter(named)) if len(named) == 1 else _space(header.group(1))
                year = None
            elif len(mentioned) == 1:
                new_owner = next(iter(mentioned))
                if new_owner != owner:
                    year = None
                owner = new_owner
            elif len(mentioned) > 1:
                owner = None
                year = None
            year_refs = set(_YEAR.findall(text))
            if len(year_refs) == 1:
                year = int(next(iter(year_refs)))
            elif len(year_refs) > 1:
                year = None
            if owner is not None and owner != target:
                continue
            if owner is None and len(names) > 1:
                continue
            if len(mentioned) > 1 or len(year_refs) > 1:
                continue
            if year is not None and year != tax_year:
                continue
            if (
                year is None
                and len(years) > 1
                and not (owner == target and field["id"] in _SHARED_CLIENT_FIELDS)
            ):
                continue
            if len(text) <= 2000 and _matches(field, value, text):
                matches.append((page["page"], number, text))
    if len(matches) != 1:
        return None
    page, number, quote = matches[0]
    try:
        return literal_citation(
            pages, page=page, line_start=number, line_end=number,
            quote=quote, media_type=media_type,
        )
    except DocumentError:
        return None
