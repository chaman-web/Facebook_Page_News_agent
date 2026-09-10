from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from models import VerificationStatus
from pipeline.posting_queue import PostingQueue, QueueEntry
from publisher_worker import has_publishable_work, run_worker


def _entry(status="QUEUED", verification=VerificationStatus.VERIFIED.value):
    now = datetime.now(timezone.utc).isoformat()
    return QueueEntry(
        title="Verified queued story",
        source_name="Example",
        source_url="https://example.com/story",
        category="world",
        score=75,
        queued_at=now,
        source_published_at=now,
        status=status,
        verification_status=verification,
    )


def test_review_only_queue_does_not_wake_publisher():
    queue = SimpleNamespace(_entries=[_entry(status="REVIEW_REQUIRED")])
    assert not has_publishable_work(queue)


def test_verified_tier_queue_wakes_publisher():
    queue = SimpleNamespace(_entries=[_entry()])
    assert has_publishable_work(queue)


def test_worker_exits_without_starting_agent_when_queue_is_empty():
    empty = SimpleNamespace(_entries=[])
    run = Mock()
    with patch("publisher_worker.PostingQueue", return_value=empty):
        assert run_worker(run=run) == 0
    run.assert_not_called()


def test_worker_runs_exactly_one_publisher_cycle_when_queue_has_work():
    ready = SimpleNamespace(_entries=[_entry()])
    run = Mock(return_value=SimpleNamespace(returncode=0))
    with patch("publisher_worker.PostingQueue", return_value=ready):
        assert run_worker(run=run) == 0
    run.assert_called_once()
