import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from src.preparation.review_events import (
    NativeAddress, ReviewAccess, ReviewSelectionSession, ReviewStreamConflict, ReviewUnavailable,
)


ACCESS = ReviewAccess("practice-firm", "preparer", "return")
DRAFT = "private-native-draft"


def session(seconds: float = 30):
    return ReviewSelectionSession(
        ACCESS, input_revision="v1", draft_key=DRAFT,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds),
        bindings={NativeAddress("w2", "w2-a", "wages"): "wages-a",
                  NativeAddress("w2", "w2-b", "wages"): "wages-b"},
    )


def test_snapshot_and_distinct_events_are_atomic_in_rapid_record_navigation():
    async def verify():
        active = session()
        active.native_selection((DRAFT, "w2", "w2-a", "wages"))
        async with active.subscribe(ACCESS, active.id) as events:
            active.native_selection((DRAFT, "w2", "w2-b", "wages"))
            active.native_selection((DRAFT, "w2", "w2-b", "wages"))
            active.native_selection((DRAFT, "w2", "w2-b", "unknown"))
            active.native_selection(("foreign-draft", "w2", "w2-a", "wages"))
            result = [await anext(events) for _ in range(4)]
            assert [(item.sequence, item.binding_id, item.status) for item in result] == [
                (1, "wages-a", "selected"), (2, "wages-b", "selected"),
                (3, None, "unmapped"), (4, None, "detached"),
            ]
        active.native_selection((DRAFT, "w2", "w2-a", "wages"))
        active.native_selection((DRAFT, "w2", "w2-b", "wages"))
        async with active.subscribe(ACCESS, active.id) as resumed:
            snapshot = await anext(resumed)
            assert snapshot.sequence == 6 and snapshot.binding_id == "wages-b"
            active.close()
            with pytest.raises(StopAsyncIteration):
                await anext(resumed)

    asyncio.run(asyncio.wait_for(verify(), timeout=2))


def test_second_http_subscriber_conflicts_without_evicting_first_or_blocking_reconnect():
    async def verify():
        active = session()
        first = active.event_response(ACCESS, active.id)
        with pytest.raises(ReviewStreamConflict):
            active.event_response(ACCESS, active.id)
        snapshot = await anext(first.body_iterator)
        assert '"sequence":0' in snapshot
        active.native_selection((DRAFT, "w2", "w2-a", "wages"))
        assert '"bindingId":"wages-a"' in await anext(first.body_iterator)
        await first.body_iterator.aclose()
        async with active.subscribe(ACCESS, active.id) as resumed:
            assert (await anext(resumed)).binding_id == "wages-a"

    asyncio.run(asyncio.wait_for(verify(), timeout=2))


def test_stream_access_and_review_session_expire_independently_of_page_focus():
    async def verify():
        active = session(seconds=0.15)
        for denied in (replace(ACCESS, preparer_id="other"), replace(ACCESS, return_id="other"),
                       replace(ACCESS, firm_id="other")):
            with pytest.raises(ReviewUnavailable):
                active.event_response(denied, active.id)
        with pytest.raises(ReviewUnavailable):
            active.event_response(ACCESS, "different-session")
        response = active.event_response(ACCESS, active.id)
        line = await anext(response.body_iterator)
        assert line.startswith("event: selection\ndata: ")
        assert json.loads(line.split("data: ")[1]) == {
            "reviewSessionId": active.id, "returnId": "return", "inputRevision": "v1",
            "sequence": 0, "bindingId": None, "status": "detached",
        }
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
        with pytest.raises(ReviewUnavailable):
            active.event_response(ACCESS, active.id)

    asyncio.run(asyncio.wait_for(verify(), timeout=2))


def test_pipe_loss_detaches_previous_binding_and_rejects_gaps_and_late_notices():
    async def verify():
        active = session()
        pipe = asyncio.StreamReader(limit=2048)
        async with active.subscribe(ACCESS, active.id) as events:
            initial = await anext(events)
            assert initial.status == "detached"
            for sequence, record in ((1, "w2-a"), (1, "w2-b"), (3, "w2-b")):
                notice = {"sequence": sequence, "draftKey": DRAFT, "section": "w2", "record": record, "field": "wages"}
                pipe.feed_data(json.dumps(notice).encode() + b"\n")
            pipe.feed_eof()
            await active.relay(pipe)
            matching = await anext(events)
            assert matching.binding_id == "wages-a"
            final = await anext(events)
            assert final.status == "detached" and final.binding_id is None and final.sequence == 2

    asyncio.run(asyncio.wait_for(verify(), timeout=2))
