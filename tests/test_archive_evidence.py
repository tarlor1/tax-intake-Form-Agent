"""Synthetic legacy data remains read-only and navigable after presentation migration."""

import asyncio
import copy
import json
import sqlite3

import pytest

from review_fixtures import candidate, legacy_scenarios
from src.sigma_demo.pdf import source
from src.preparation.legacy_inspection import LegacyInspection
from src.review_store import ReviewStore


@pytest.mark.parametrize("scenario_index", (0, 1))
def test_migrated_phone_gets_distinct_presentation_links_without_altering_archive(tmp_path, scenario_index):
    payload = copy.deepcopy(legacy_scenarios()[scenario_index]["detail"])
    phone = next(item for item in payload["fields"] if item["id"] in {"phone", "contact-phone"})
    document, pdf, anchors = source("phone-original", "Example contact sheet", ["Phone: 503-555-0101"], version=3)
    row = candidate(phone["id"], "intake", "503-555-0101", anchors[0])
    row.update(documentId=document["id"], documentVersion=document["version"])
    phone.update(value=row["value"], selectedCandidateId=row["id"], candidates=[row], citations=anchors)
    payload["documents"].append(document)
    store = ReviewStore(tmp_path)
    with sqlite3.connect(store.database_path) as db:
        db.execute("INSERT INTO review_returns VALUES (?, ?, ?)", (payload["id"], json.dumps(payload), "archived"))
        db.execute("INSERT INTO review_documents VALUES (?, ?, ?)", (payload["id"], document["id"], pdf))
    before = store.database_path.read_bytes()

    async def inspect():
        archive = LegacyInspection(tmp_path)
        access = await archive.access(payload["id"])
        view = await archive.evidence(access, None)
        phone_fields = tuple(item for item in view.fields if any(
            citation.evidence.original.document_id == document["id"]
            for candidate in item.candidates for citation in candidate.citations
        ))
        assert len(phone_fields) == 2
        assert phone_fields[0].id == "phone" and phone_fields[1].id != "phone"
        assert len({item.id for item in view.fields}) == len(view.fields)
        assert phone_fields[0].candidates[0].id != phone_fields[1].candidates[0].id
        assert phone_fields[0].used_citation_id != phone_fields[1].used_citation_id
        for item in phone_fields:
            (citation,) = item.candidates[0].citations
            assert item.used_citation_id == citation.id
            original = citation.evidence.original
            assert (original.document_id, original.version_id) == (document["id"], "3")
            assert await archive.original(access, view.input_revision, original.document_id, original.version_id) == ("application/pdf", pdf)
        assert await LegacyInspection(tmp_path).evidence(access, view.input_revision) == view

    asyncio.run(inspect())
    assert store.database_path.read_bytes() == before
