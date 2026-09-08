"""Regression tests for posting-queue lifecycle behavior."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("NEWSAPI_KEY", "test-key")

from models import Story, VerificationStatus  # noqa: E402
from pipeline.posting_queue import PostingQueue, QueueEntry, Route  # noqa: E402


def test_expire_stale_persists_all_required_downgrades(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    queued_at = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()

    with (
        patch("pipeline.posting_queue.QUEUE_FILE", queue_path),
        patch("pipeline.posting_queue.AUDIT_FILE", audit_path),
    ):
        queue = PostingQueue()
        queue._entries = [
            QueueEntry(
                title="Old breaking story",
                source_name="Test Source",
                source_url="https://example.com/old-breaking",
                category="breaking",
                score=90,
                queued_at=queued_at,
                is_breaking=True,
                category_tier=1,
                route=Route.PUBLISH_NOW.value,
            )
        ]
        queue._save()

        assert queue.expire_stale() == 1

        reloaded = PostingQueue()
        entry = reloaded._entries[0]
        assert entry.route == Route.SCHEDULE.value
        assert entry.downgraded_from == Route.PUBLISH_NOW.value
        assert entry.is_breaking is False


def test_provisional_story_is_saved_for_review_not_auto_queue(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    story = Story(
        title="Major developing story from one strong source",
        source_name="Reuters",
        source_url="https://reuters.com/developing",
        published_at=datetime.now(timezone.utc),
        raw_summary="A major developing event has been reported by one source.",
        source_tier=1,
        verification_status=VerificationStatus.UNVERIFIED,
        verification_score=55,
        verification_reason="Awaiting an independent source.",
    )

    with (
        patch("pipeline.posting_queue.QUEUE_FILE", queue_path),
        patch("pipeline.posting_queue.AUDIT_FILE", audit_path),
    ):
        queue = PostingQueue()
        queue.add(story, score=92, effective_tier=1)

        assert queue._entries[0].score == 92
        assert queue._entries[0].status == "REVIEW_REQUIRED"
        assert queue.queued_count() == 0
