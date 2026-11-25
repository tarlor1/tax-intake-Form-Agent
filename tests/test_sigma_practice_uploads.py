"""Synthetic upstream persists each immutable upload and its event atomically."""

import pytest

from sigma_practice_uploads import PracticeUploads, UploadCommitted, UploadSlot

FIRM = "practice-firm"
GRANT = "practice-processing"


class InterruptedDelivery(Exception):
    pass


def test_upload_and_event_are_invisible_until_commit_and_roll_back_together(tmp_path):
    upstream = PracticeUploads(tmp_path / "upstream.sqlite")
    manifest = (UploadSlot("wages", "return-a", 1, GRANT),)
    with pytest.raises(InterruptedDelivery):
        with upstream.writer() as writer:
            writer.declare_batch(FIRM, "batch-a", manifest, approved_grants=(GRANT,))
            rolled_back = writer.put_file(FIRM, "batch-a", "wages", b"discarded synthetic wages")
            assert upstream.feed() == ()
            assert upstream.read_version(FIRM, rolled_back) is None
            assert upstream.candidate(UploadCommitted(1, rolled_back)) is None
            raise InterruptedDelivery

    assert upstream.feed() == ()
    assert upstream.read_version(FIRM, rolled_back) is None
    with upstream.writer() as writer:
        # The rolled-back declaration must not prevent reusing the batch ID.
        slots = (UploadSlot("ledger", "return-a", 1, GRANT),)
        writer.declare_batch(FIRM, "batch-a", slots, approved_grants=(GRANT,))
        version_id = writer.put_file(FIRM, "batch-a", "ledger", b"committed synthetic ledger")
        assert upstream.feed() == ()
    (event,) = upstream.feed()
    assert event.version_id == version_id != rolled_back
    assert upstream.read_version(FIRM, version_id) == b"committed synthetic ledger"
    assert upstream.read_version("another-firm", version_id) is None
    candidate = upstream.candidate(event)
    assert candidate is not None and candidate.slot_id == "ledger" and candidate.version_id == version_id


def test_identical_writes_publish_once_and_slots_cannot_be_replaced(tmp_path):
    upstream = PracticeUploads(tmp_path / "upstream.sqlite")
    manifest = (UploadSlot("wages", "return-a", 1, GRANT),)
    original = "Practice wages – $3,000".encode("utf-8")
    with upstream.writer() as writer:
        writer.declare_batch(FIRM, "batch-a", manifest, approved_grants=(GRANT,))
        version_id = writer.put_file(FIRM, "batch-a", "wages", original)
        assert writer.put_file(FIRM, "batch-a", "wages", original) == version_id
    baseline = upstream.feed()
    assert len(baseline) == 1

    with upstream.writer() as writer:
        writer.declare_batch(FIRM, "batch-a", manifest, approved_grants=(GRANT,))
        assert writer.put_file(FIRM, "batch-a", "wages", original) == version_id
        with pytest.raises(ValueError, match="Cannot replace"):
            writer.put_file(FIRM, "batch-a", "wages", b"different bytes")
        with pytest.raises(ValueError, match="not declared"):
            writer.put_file(FIRM, "batch-a", "other-slot", original)
        with pytest.raises(ValueError, match="Cannot change"):
            writer.declare_batch(FIRM, "batch-a", manifest)

    assert upstream.feed() == baseline
    assert upstream.read_version(FIRM, version_id) == original


def test_one_committed_file_is_visible_while_another_slot_is_missing(tmp_path):
    upstream = PracticeUploads(tmp_path / "upstream.sqlite")
    slots = (UploadSlot("wages", "return-a", 1, GRANT), UploadSlot("ledger", "return-b", 1, GRANT))
    with upstream.writer() as writer:
        writer.declare_batch(FIRM, "drop", slots, approved_grants=(GRANT,))
        version = writer.put_file(FIRM, "drop", "wages", b"firm A wages")
    (event,) = upstream.feed()
    candidate = upstream.candidate(event)
    assert candidate is not None and candidate.version_id == version
    assert candidate.return_id == "return-a" and candidate.authorized
