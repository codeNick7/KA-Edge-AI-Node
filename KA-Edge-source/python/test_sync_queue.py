import time

import pytest

import sync_queue


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "sync_queue.db")


def test_enqueue_and_get_pending(db_path):
    queue_id = sync_queue.enqueue("evidence", "edge-node-01|A|1", {"flag": "stable"}, db_path=db_path)
    assert isinstance(queue_id, int)

    pending = sync_queue.get_pending("evidence", db_path=db_path)
    assert len(pending) == 1
    assert pending[0]["id"] == queue_id
    assert pending[0]["payload"] == {"flag": "stable"}
    assert pending[0]["attempts"] == 0


def test_enqueue_is_idempotent_by_key(db_path):
    sync_queue.enqueue("evidence", "edge-node-01|A|1", {"flag": "stable"}, db_path=db_path)
    duplicate = sync_queue.enqueue("evidence", "edge-node-01|A|1", {"flag": "moisture_low"}, db_path=db_path)

    assert duplicate is None
    pending = sync_queue.get_pending("evidence", db_path=db_path)
    assert len(pending) == 1
    assert pending[0]["payload"] == {"flag": "stable"}  # original wins, not overwritten


def test_mark_synced_removes_from_pending(db_path):
    sync_queue.enqueue("evidence", "edge-node-01|A|1", {"flag": "stable"}, db_path=db_path)
    pending = sync_queue.get_pending("evidence", db_path=db_path)
    sync_queue.mark_synced(pending[0]["id"], db_path=db_path)

    assert sync_queue.get_pending("evidence", db_path=db_path) == []
    assert sync_queue.pending_count("evidence", db_path=db_path) == 0


def test_mark_failed_applies_backoff_and_excludes_from_pending(db_path):
    sync_queue.enqueue("evidence", "edge-node-01|A|1", {"flag": "stable"}, db_path=db_path)
    pending = sync_queue.get_pending("evidence", db_path=db_path)
    sync_queue.mark_failed(pending[0]["id"], "connection refused", db_path=db_path)

    # Backoff pushes next_attempt_at into the future -- not immediately retryable.
    assert sync_queue.get_pending("evidence", db_path=db_path) == []
    assert sync_queue.pending_count("evidence", db_path=db_path) == 1  # still queued, not lost


def test_different_kinds_do_not_collide(db_path):
    sync_queue.enqueue("evidence", "edge-node-01|A|1", {"flag": "stable"}, db_path=db_path)
    sync_queue.enqueue("raw_batch", "edge-node-01|A|1", {"reading": 1}, db_path=db_path)

    assert len(sync_queue.get_pending("evidence", db_path=db_path)) == 1
    assert len(sync_queue.get_pending("raw_batch", db_path=db_path)) == 1


def test_survives_reconnect_reboot_simulation(db_path):
    # Simulate a board reboot: enqueue, then reconnect fresh (new sqlite3
    # connection, as would happen on process restart) and confirm the
    # record is still there.
    sync_queue.enqueue("evidence", "edge-node-01|A|1", {"flag": "stable"}, db_path=db_path)
    pending = sync_queue.get_pending("evidence", db_path=db_path)
    assert len(pending) == 1
