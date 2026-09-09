"""Regression tests for posting-queue lifecycle behavior."""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("NEWSAPI_KEY", "test-key")

from models import Story, VerificationStatus  # noqa: E402
from pipeline.posting_queue import PostingQueue, QueueEntry, Route, route_story  # noqa: E402


def test_routing_is_immediate_moderate_or_reject():
    assert route_story(80, 3) == Route.PUBLISH_NOW
    assert route_story(79.9, 1) == Route.SCHEDULE
    assert route_story(65, 3) == Route.SCHEDULE
    assert route_story(64.9, 1) == Route.REJECT
    assert route_story(65, 3, impact_score=10) == Route.PUBLISH_NOW


def test_high_impact_metadata_survives_retry_queue(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    story = Story(
        title="Major emergency affects millions",
        source_name="Reuters",
        source_url="https://reuters.com/high-impact",
        published_at=datetime.now(timezone.utc),
        raw_summary="A verified emergency affects millions of people.",
        source_tier=1,
        verification_status=VerificationStatus.VERIFIED,
        verification_score=90,
        image_provenance="pollinations_ai",
        image_credit="Pollinations.ai",
        image_is_synthetic=True,
        card_description="The emergency disrupted essential services across the region.",
    )

    with (
        patch("pipeline.posting_queue.QUEUE_FILE", queue_path),
        patch("pipeline.posting_queue.AUDIT_FILE", audit_path),
    ):
        queue = PostingQueue()
        queue.add(
            story,
            score=68,
            effective_tier=1,
            impact_score=10,
            impact_reasons=["population-policy", "critical-infrastructure"],
        )

        reloaded = PostingQueue()
        entry = reloaded._entries[0]
        assert entry.route == Route.PUBLISH_NOW.value
        assert entry.is_breaking is True
        assert entry.impact_score == 10
        assert entry.impact_reasons == ["population-policy", "critical-infrastructure"]
        assert entry.routing_reason == "impact score >= 10"
        assert entry.image_provenance == "pollinations_ai"
        assert entry.image_is_synthetic is True
        assert entry.card_description == story.card_description


def test_refetch_promotes_existing_tier2_entry_to_tier1(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    story = Story(
        title="Government issues major infrastructure update",
        source_name="Reuters",
        source_url="https://reuters.com/developing-update",
        published_at=datetime.now(timezone.utc),
        raw_summary="Officials issued a verified infrastructure update.",
        verification_status=VerificationStatus.VERIFIED,
        verification_score=85,
    )
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        queue.add(story, score=72, post_content="Initial verified update")
        queued_at = queue._entries[0].queued_at

        story.verification_score = 96
        queue.add(
            story,
            score=84,
            impact_score=11,
            impact_reasons=["critical-infrastructure"],
            post_content="Newly corroborated major update",
        )

        assert len(queue._entries) == 1
        entry = queue._entries[0]
        assert entry.queued_at == queued_at
        assert entry.route == Route.PUBLISH_NOW.value
        assert entry.score == 84
        assert entry.impact_score == 11
        assert entry.post_content == "Newly corroborated major update"


def test_meaningful_event_update_is_not_blocked_as_queue_duplicate(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    now = datetime.now(timezone.utc)
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        queue._entries = [QueueEntry(
            "Earthquake death toll rises to 20 after rescue operation", "Reuters",
            "https://reuters.com/first", "world", 76, now.isoformat(), status="PUBLISHED",
            published_at=now.isoformat(),
        )]
        update = Story(
            title="Earthquake death toll rises to 40 after rescue operation",
            source_name="Reuters", source_url="https://reuters.com/update",
            published_at=now, raw_summary="Officials confirmed an updated toll.",
            verification_status=VerificationStatus.VERIFIED, verification_score=95,
        )
        queue.add(update, score=82, post_content="Confirmed update")
        assert any(entry.source_url == update.source_url for entry in queue._entries)


def test_entries_remain_queued_until_48_hour_freshness_limit(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    queued_at = (datetime.now(timezone.utc) - timedelta(hours=47)).isoformat()

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

        assert queue.expire_stale() == 0

        reloaded = PostingQueue()
        entry = reloaded._entries[0]
        assert entry.route == Route.PUBLISH_NOW.value
        assert entry.status == "QUEUED"


def test_dry_run_style_cleanup_can_be_kept_in_memory(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        queue._entries = [QueueEntry("Old", "A", "https://a.test/old", "world", 70, old)]
        queue._save()
        original = queue_path.read_text(encoding="utf-8")
        queue._save = lambda: None
        queue._audit = lambda *args, **kwargs: None
        queue.purge_old(max_age_hours=48)
        assert queue._entries == []
        assert queue_path.read_text(encoding="utf-8") == original


def test_regular_queue_is_global_score_first_and_retries_are_preserved(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    now = datetime.now(timezone.utc)
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        lower = QueueEntry("Lower", "A", "https://a.test", "world", 70, now.isoformat())
        higher = QueueEntry("Higher", "B", "https://b.test", "business", 79, now.isoformat())
        queue._entries = [lower, higher]
        assert queue.deserves_publishing(lower, None)[0] is False
        assert queue.deserves_publishing(higher, None)[0] is True
        queue.mark_retry(higher, "temporary Graph API failure")
        assert higher.status == "QUEUED"
        assert higher.publish_attempts == 1
        assert higher.next_retry_at is not None
        assert queue.deserves_publishing(higher, None)[0] is False


def test_tier2_age_bonus_only_reorders_near_equal_scores(tmp_path):
    now = datetime.now(timezone.utc)
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        older = QueueEntry("Older", "A", "https://a.test/old", "world", 72,
                           (now - timedelta(hours=24)).isoformat())
        newer = QueueEntry("Newer", "B", "https://b.test/new", "world", 73,
                           now.isoformat())
        assert queue.effective_tier2_score(older, now) == 74
        assert min([older, newer], key=queue.priority_key) is older


def test_fresh_reconciliation_demotes_tier1_and_rejects_below_floor(tmp_path):
    now = datetime.now(timezone.utc)
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    story = Story("Developing event", "Reuters", "https://r.test/event", now,
                  "A developing verified event.", verification_status=VerificationStatus.VERIFIED,
                  verification_score=90)
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        queue.add(story, 85, impact_score=10)
        assert queue.reconcile_candidate(story, 74, impact_score=0)
        assert queue._entries[0].route == Route.SCHEDULE.value
        assert queue.reconcile_candidate(story, 60, impact_score=0)
        assert queue._entries[0].status == "EXPIRED"


def test_tier1_priority_prefers_impact_then_freshness(tmp_path):
    now = datetime.now(timezone.utc)
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        lower_impact = QueueEntry("A", "A", "https://a.test", "world", 99, now.isoformat(),
                                  route=Route.PUBLISH_NOW.value, impact_score=10,
                                  source_published_at=now.isoformat())
        higher_impact = QueueEntry("B", "B", "https://b.test", "world", 82, now.isoformat(),
                                   route=Route.PUBLISH_NOW.value, impact_score=12,
                                   source_published_at=(now - timedelta(hours=1)).isoformat())
        assert min([lower_impact, higher_impact], key=queue.priority_key) is higher_impact


def test_tier1_bypasses_regular_daily_limit_but_obeys_global_gap(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    now = datetime.now(timezone.utc)
    local_today = datetime.now().astimezone().replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    with patch("pipeline.posting_queue.QUEUE_FILE", queue_path), patch("pipeline.posting_queue.AUDIT_FILE", audit_path):
        queue = PostingQueue()
        queue._entries = [
            QueueEntry(f"Regular {i}", "A", f"https://a.test/{i}", "world", 70,
                       now.isoformat(), route=Route.SCHEDULE.value, status="PUBLISHED",
                       published_at=local_today.isoformat())
            for i in range(12)
        ]
        breaking = QueueEntry("Breaking", "B", "https://b.test", "breaking", 90,
                              now.isoformat(), route=Route.PUBLISH_NOW.value)
        assert queue.can_publish_today() is False
        assert queue.deserves_publishing(breaking, now - timedelta(minutes=11))[0] is True
        assert queue.deserves_publishing(breaking, now - timedelta(minutes=5))[0] is False


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


def test_legacy_queue_infers_branded_fallback_provenance(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    queue_path.write_text(json.dumps({"entries": [{
        "title": "Legacy fallback story",
        "source_name": "Reuters",
        "source_url": "https://reuters.com/legacy-fallback",
        "category": "world",
        "score": 75,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "image_path": str(tmp_path / "Legacy_story_fallback.jpg"),
    }]}), encoding="utf-8")

    with (
        patch("pipeline.posting_queue.QUEUE_FILE", queue_path),
        patch("pipeline.posting_queue.AUDIT_FILE", audit_path),
    ):
        entry = PostingQueue()._entries[0]

    assert entry.image_provenance == "branded_fallback"
    assert entry.image_credit == "Global Pulse News"


def test_legacy_queue_marks_unknown_existing_image_for_replacement(tmp_path):
    queue_path = tmp_path / "posting_queue.json"
    audit_path = tmp_path / "posting_decisions.jsonl"
    queue_path.write_text(json.dumps({"entries": [{
        "title": "Legacy external image story",
        "source_name": "Reuters",
        "source_url": "https://reuters.com/legacy-image",
        "category": "world",
        "score": 75,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "image_path": str(tmp_path / "Legacy_story.jpg"),
        "image_provenance": "",
    }]}), encoding="utf-8")

    with (
        patch("pipeline.posting_queue.QUEUE_FILE", queue_path),
        patch("pipeline.posting_queue.AUDIT_FILE", audit_path),
    ):
        entry = PostingQueue()._entries[0]

    assert entry.image_provenance == "legacy_unknown"
    assert entry.image_credit == ""
