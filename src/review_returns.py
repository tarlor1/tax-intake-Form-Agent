"""Blank review state for the original external client intake controls."""

from __future__ import annotations

import copy

from .intake_contract import CONTRACT_VERSION, field_definitions, normalize_value


def blank_return(
    return_id: str, client_id: str, client_name: str, tax_year: int, return_type: str
) -> dict:
    fields = []
    for definition in field_definitions():
        item = {
            **definition,
            "value": None,
            "revision": 1,
            "status": "blocked",
            "conflict": False,
            "selectedCandidateId": None,
            "candidates": [],
            "citations": [],
            "correctionReason": None,
            "valueOrigin": None,
        }
        if item["id"] in {"full_name", "tax_year"}:
            try:
                item["value"] = normalize_value(
                    item, client_name if item["id"] == "full_name" else str(tax_year)
                )
            except ValueError:
                pass  # Historical identity remains on the workspace, not in an invalid native control.
            if item["value"] is not None:
                item.update(valueOrigin="workspace", status="needs_review")
        fields.append(item)
    return {
        "id": return_id,
        "clientId": client_id,
        "clientName": client_name,
        "taxYear": tax_year,
        "returnType": return_type,
        "destination": "Client intake",
        "intakeContractVersion": CONTRACT_VERSION,
        "revision": 1,
        "activeFieldId": "full_name",
        "fields": fields,
        "legacyFields": [],
        "documents": [],
        "events": [],
    }


def migrate_return(detail: dict) -> dict:
    """Archive old worksheets intact; reuse only compatible, validated phone facts."""
    if detail.get("intakeContractVersion") == CONTRACT_VERSION:
        # Native date/year maxima advance while a workspace can remain open for years.
        definitions = {item["id"]: item for item in field_definitions()}
        for item in detail["fields"]:
            if item["id"] in definitions:
                item.update(definitions[item["id"]])
        detail.setdefault("legacyFields", [])
        return detail

    previous = detail["fields"]
    replacement = blank_return(
        detail["id"],
        detail["clientId"],
        detail["clientName"],
        detail["taxYear"],
        detail["returnType"],
    )
    detail["legacyFields"] = detail.get("legacyFields", []) + copy.deepcopy(previous)

    phone = next((item for item in previous if item["id"] in {"phone", "contact-phone"}), None)
    if phone is not None:
        target = next(item for item in replacement["fields"] if item["id"] == "phone")
        metadata = {
            key: copy.deepcopy(target[key])
            for key in (
                "id",
                "label",
                "section",
                "kind",
                "controlType",
                "required",
                "options",
                "dependencies",
                "derivation",
            )
        }
        try:
            normalized = normalize_value(target, phone.get("value"))
            candidates = copy.deepcopy(phone.get("candidates", []))
            for candidate in candidates:
                candidate["value"] = normalize_value(target, candidate["value"])
                if candidate["value"] is None:
                    raise ValueError("Missing candidate")
            # Candidate/citation IDs are opaque scoped evidence identities; retain them so all
            # selected references remain valid. Only the actual field ID changes for contact-phone.
            target.update(copy.deepcopy(phone))
            target.update(id="phone", value=normalized, candidates=candidates)
            target.update(metadata)
        except (ValueError, KeyError, TypeError):
            pass  # The complete unparseable original remains visible in legacyFields.

    for document in detail["documents"]:
        if document.get("extractionStatus") == "completed":
            document["extractionStatus"] = "pending"

    detail.update(
        fields=replacement["fields"],
        destination="Client intake",
        intakeContractVersion=CONTRACT_VERSION,
        activeFieldId=detail["activeFieldId"]
        if detail["activeFieldId"] == "phone"
        else "full_name",
    )

    return detail
