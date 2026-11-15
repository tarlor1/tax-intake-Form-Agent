"""Reviewable draft and preparer values for the unchanged hosted intake form."""

from __future__ import annotations

from .intake_contract import decode_value, field_definitions, normalize_value


def _has_evidence(field: dict, documents: dict[str, dict]) -> bool:
    return any(
        isinstance(citation, dict)
        and not citation.get("localMatch")
        and citation.get("documentId") in documents
        and type(citation.get("page")) is int
        and 1 <= citation["page"] <= documents[citation["documentId"]].get("pageCount", 0)
        and isinstance(citation.get("quote"), str)
        and bool(citation["quote"].strip())
        for citation in field.get("citations", [])
    )


def _has_document_candidate(field: dict, value: str, documents: dict[str, dict]) -> bool:
    """A draft references an owned original, not an invented literal citation."""
    if field.get("valueOrigin") not in {"ai", "source"}:
        return False
    return any(
        candidate.get("id") == field.get("selectedCandidateId")
        and candidate.get("value") == value
        and isinstance(candidate.get("documentId"), str)
        and candidate["documentId"] in documents
        and (field.get("valueOrigin") != "ai" or candidate.get("provenance") == "gemini")
        for candidate in field.get("candidates", [])
    )


def fillable_values(detail: dict) -> tuple[dict[str, str | list[str]], list[str]]:
    """Use owned-source drafts, literal evidence, human entries, or workspace identity.

    Unknown/legacy fields never map to unrelated controls. Missing optional inputs
    stay blank without being treated as submission requirements.
    """
    documents = {item["id"]: item for item in detail.get("documents", [])}
    fields = {item["id"]: item for item in detail.get("fields", [])}
    values: dict[str, str | list[str]] = {}
    issues: list[str] = []
    identity = {"full_name": detail.get("clientName"), "tax_year": str(detail.get("taxYear", ""))}

    for definition in field_definitions():
        field = fields.get(definition["id"], {})
        value = field.get("value")
        if isinstance(value, str) and ("\x00" in value or len(value) > 10000):
            issues.append(f"{definition['label']}: unsupported value; left blank.")
            continue

        try:
            normalized = normalize_value(definition, value)
        except (ValueError, TypeError):
            issues.append(f"{definition['label']}: unsupported value; left blank.")
            continue

        if normalized is None or normalized == "":
            if definition.get("required"):
                issues.append(f"{definition['label']}: required information is missing.")
            continue

        if field.get("conflict"):
            issues.append(
                f"{definition['label']}: conflicting sources; choose a source in the real form."
            )
            continue

        origin = field.get("valueOrigin")
        supported = (
            origin == "preparer"
            or _has_evidence(field, documents)
            or _has_document_candidate(field, normalized, documents)
            or (
                origin == "workspace"
                and definition["id"] in identity
                and normalized == identity[definition["id"]]
            )
        )
        if not supported:
            issues.append(
                f"{definition['label']}: no usable source document or preparer entry; left blank."
            )
            continue

        values[definition["id"]] = decode_value(definition, normalized)

    return values, issues


def form_payload(detail: dict) -> dict[str, str | list[str]]:
    """Expected native collect() payload, including blank scalar/checkbox inputs."""
    values, _ = fillable_values(detail)
    return {
        item["id"]: values.get(item["id"], decode_value(item, None)) for item in field_definitions()
    }
