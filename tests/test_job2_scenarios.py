"""
tests/test_job2_scenarios.py — Comprehensive Job 2 (publish) scenario tests.

Covers every failure/edge scenario identified:
  1.  Queue empty              → worker exits 0, no agent call
  2.  REVIEW_REQUIRED only     → not publishable, worker exits 0
  3.  Happy path               → publishes one story, exits 0
  4.  Facebook API failure     → worker exits non-zero
  5.  Retry backoff active     → story with future next_retry_at is deferred
  6.  Retry backoff expired    → story published after backoff window passes
  7.  Tier 1 priority gate     → Tier 2 deferred while Tier 1 is queued
  8.  30-min interval guard    → post blocked when gap hasn't elapsed
  9.  Tier 2 daily limit       → blocked when 12 Tier 2 posts already done today
  10. TTL expiry               → story older than 48 h is expired, not published
  11. Queue save/load round-trip → state survives disk write and reload
  12. Corrupted queue file      → starts fresh without crashing
  13. Duplicate topic block     → same-topic story not added to queue twice
  14. Score below floor 65      → REJECT, never queued
  15. Unverified story          → REVIEW_REQUIRED, never auto-published
  16. Reconciliation            → Tier 1 demoted to Tier 2 on fresh lower score
  17. mark_retry backoff growth → exponential delay, capped at 120 min
  18. Worker propagates exit code
  19. Single cycle per run      → agent called exactly once per run_worker call
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import VerificationStatus
from pipeline.posting_queue import (
    TIER2_DAILY_LIMIT,
    PostingQueue,
    QueueEntry,
    Route,
    route_story,
)
from publisher_worker import has_publishable_work, run_worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(hours: float = 0, minutes: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes)).isoformat()


def _future_iso(minutes: float = 10) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _entry(
    status: str = "QUEUED",
    route: str = Route.PUBLISH_NOW.value,
    score: float = 85.0,
    hours_ago: float = 1.0,
    next_retry_at: str | None = None,
    published_at: str | None = None,
    category_tier: int = 1,
    source_url: str = "https://bbc.com/story",
    title: str = "Test story",
    verification: str = VerificationStatus.VERIFIED.value,
) -> QueueEntry:
    ts = _ago_iso(hours=hours_ago)
    return QueueEntry(
        title=title,
        source_name="BBC",
        source_url=source_url,
        category="world",
        score=score,
        queued_at=ts,
        source_published_at=ts,
        status=status,
        route=route,
        category_tier=category_tier,
        verification_status=verification,
        verification_score=85.0,
        next_retry_at=next_retry_at,
        published_at=published_at,
    )


def _queue(entries: list[QueueEntry]) -> SimpleNamespace:
    """Simple namespace that satisfies has_publishable_work / run_worker."""
    return SimpleNamespace(_entries=entries)


def _bare_queue(entries: list[QueueEntry]) -> PostingQueue:
    """PostingQueue instance with _entries pre-populated, no file I/O."""
    q = PostingQueue.__new__(PostingQueue)
    q._entries = entries
    return q


# ---------------------------------------------------------------------------
# Scenario 1: Queue empty
# ---------------------------------------------------------------------------

class TestEmptyQueue:
    def test_has_no_publishable_work(self):
        assert not has_publishable_work(_queue([]))

    def test_worker_exits_0_without_calling_agent(self):
        run = Mock()
        with patch("publisher_worker.PostingQueue", return_value=_queue([])):
            result = run_worker(run=run)
        assert result == 0
        run.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 2: REVIEW_REQUIRED only
# ---------------------------------------------------------------------------

class TestReviewRequiredOnly:
    def test_review_required_not_publishable(self):
        assert not has_publishable_work(_queue([_entry(status="REVIEW_REQUIRED")]))

    def test_unverified_queued_not_publishable(self):
        e = _entry(verification=VerificationStatus.UNVERIFIED.value)
        assert not has_publishable_work(_queue([e]))

    def test_worker_idle_no_agent_call(self):
        run = Mock()
        q = _queue([_entry(status="REVIEW_REQUIRED")])
        with patch("publisher_worker.PostingQueue", return_value=q):
            assert run_worker(run=run) == 0
        run.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 3: Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_verified_queued_is_publishable(self):
        assert has_publishable_work(_queue([_entry()]))

    def test_worker_calls_agent_and_returns_0(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))
        with patch("publisher_worker.PostingQueue", return_value=_queue([_entry()])):
            assert run_worker(run=run) == 0
        run.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 4: Facebook API failure
# ---------------------------------------------------------------------------

class TestFacebookFailure:
    def test_worker_returns_1_on_agent_failure(self):
        run = Mock(return_value=SimpleNamespace(returncode=1))
        with patch("publisher_worker.PostingQueue", return_value=_queue([_entry()])):
            assert run_worker(run=run) == 1

    def test_worker_returns_2_on_agent_error(self):
        run = Mock(return_value=SimpleNamespace(returncode=2))
        with patch("publisher_worker.PostingQueue", return_value=_queue([_entry()])):
            assert run_worker(run=run) == 2


# ---------------------------------------------------------------------------
# Scenario 5: Retry backoff active — story deferred
# ---------------------------------------------------------------------------

class TestRetryBackoffActive:
    def test_story_in_backoff_is_deferred(self):
        e = _entry(next_retry_at=_future_iso(minutes=20))
        q = _bare_queue([e])
        last_dt = datetime.fromisoformat(_ago_iso(minutes=40))
        ok, reason = q.deserves_publishing(e, last_dt)
        assert not ok
        assert "retry" in reason.lower() or "backoff" in reason.lower()


# ---------------------------------------------------------------------------
# Scenario 6: Retry backoff expired — story eligible
# ---------------------------------------------------------------------------

class TestRetryBackoffExpired:
    def test_story_past_backoff_is_eligible(self):
        e = _entry(next_retry_at=_ago_iso(minutes=5))
        q = _bare_queue([e])
        last_dt = datetime.fromisoformat(_ago_iso(minutes=40))
        ok, _ = q.deserves_publishing(e, last_dt)
        assert ok


# ---------------------------------------------------------------------------
# Scenario 7: Tier 1 priority gate
# ---------------------------------------------------------------------------

class TestTier1PriorityGate:
    def test_tier2_blocked_while_tier1_queued(self):
        tier1 = _entry(route=Route.PUBLISH_NOW.value, score=85.0, category_tier=1)
        tier2 = _entry(
            route=Route.SCHEDULE.value, score=70.0, category_tier=2,
            source_url="https://cnn.com/story", title="Tier 2 story",
            hours_ago=0.5,
        )
        q = _bare_queue([tier1, tier2])
        last_dt = datetime.fromisoformat(_ago_iso(minutes=40))
        ok, reason = q.deserves_publishing(tier2, last_dt)
        assert not ok
        assert "tier 1" in reason.lower() or "priority" in reason.lower()


# ---------------------------------------------------------------------------
# Scenario 8: 30-min interval guard
# ---------------------------------------------------------------------------

class TestIntervalGuard:
    def test_tier1_blocked_within_30min(self):
        e = _entry(route=Route.PUBLISH_NOW.value, score=85.0)
        q = _bare_queue([e])
        last_dt = datetime.fromisoformat(_ago_iso(minutes=10))
        ok, reason = q.deserves_publishing(e, last_dt)
        assert not ok
        assert "remaining" in reason.lower() or "cooldown" in reason.lower()

    def test_tier1_allowed_after_30min(self):
        e = _entry(route=Route.PUBLISH_NOW.value, score=85.0)
        q = _bare_queue([e])
        last_dt = datetime.fromisoformat(_ago_iso(minutes=35))
        ok, _ = q.deserves_publishing(e, last_dt)
        assert ok

    def test_tier2_blocked_within_30min(self):
        e = _entry(route=Route.SCHEDULE.value, score=70.0, category_tier=2)
        q = _bare_queue([e])
        last_dt = datetime.fromisoformat(_ago_iso(minutes=10))
        ok, _ = q.deserves_publishing(e, last_dt)
        assert not ok


# ---------------------------------------------------------------------------
# Scenario 9: Tier 2 daily limit
# ---------------------------------------------------------------------------

class TestTier2DailyLimit:
    def test_tier2_blocked_when_limit_reached(self):
        today = datetime.now(timezone.utc).isoformat()
        published = [
            _entry(
                status="PUBLISHED",
                route=Route.SCHEDULE.value,
                category_tier=2,
                published_at=today,
                source_url=f"https://example.com/s{i}",
                title=f"Published story {i}",
            )
            for i in range(TIER2_DAILY_LIMIT)
        ]
        candidate = _entry(
            route=Route.SCHEDULE.value, score=70.0, category_tier=2,
            source_url="https://example.com/new", title="New story",
        )
        q = _bare_queue(published + [candidate])
        last_dt = datetime.fromisoformat(_ago_iso(minutes=35))
        ok, reason = q.deserves_publishing(candidate, last_dt)
        assert not ok
        assert "limit" in reason.lower() or "daily" in reason.lower()


# ---------------------------------------------------------------------------
# Scenario 10: TTL expiry
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    def test_50h_old_story_is_expired(self):
        e = _entry(hours_ago=50.0)
        q = _bare_queue([e])
        result = q._check_ttl(e, datetime.now(timezone.utc))
        assert result is not None
        assert result[0] is False
        assert e.status == "EXPIRED"

    def test_2h_old_story_is_not_expired(self):
        e = _entry(hours_ago=2.0)
        q = _bare_queue([e])
        result = q._check_ttl(e, datetime.now(timezone.utc))
        assert result is None
        assert e.status == "QUEUED"

    def test_expire_stale_counts_expired_entries(self, tmp_path):
        e = _entry(hours_ago=50.0)
        q = _bare_queue([e])
        with patch("pipeline.posting_queue.QUEUE_FILE", tmp_path / "q.json"), \
             patch("pipeline.posting_queue.AUDIT_FILE", tmp_path / "a.jsonl"):
            changed = q.expire_stale()
        assert changed == 1
        assert e.status == "EXPIRED"


# ---------------------------------------------------------------------------
# Scenario 11: Queue save/load round-trip
# ---------------------------------------------------------------------------

class TestQueuePersistence:
    def test_entry_survives_save_and_reload(self, tmp_path):
        queue_file = tmp_path / "posting_queue.json"
        audit_file = tmp_path / "audit.jsonl"

        with patch("pipeline.posting_queue.QUEUE_FILE", queue_file), \
             patch("pipeline.posting_queue.AUDIT_FILE", audit_file):
            q1 = PostingQueue()
            q1._entries = [_entry()]
            q1._save()
            q2 = PostingQueue()

        assert len(q2._entries) == 1
        assert q2._entries[0].title == "Test story"
        assert q2._entries[0].status == "QUEUED"
        assert q2._entries[0].verification_status == VerificationStatus.VERIFIED.value


# ---------------------------------------------------------------------------
# Scenario 12: Corrupted queue file
# ---------------------------------------------------------------------------

class TestCorruptedQueue:
    def test_corrupted_file_starts_fresh(self, tmp_path):
        queue_file = tmp_path / "posting_queue.json"
        queue_file.write_text("NOT VALID JSON", encoding="utf-8")

        with patch("pipeline.posting_queue.QUEUE_FILE", queue_file), \
             patch("pipeline.posting_queue.AUDIT_FILE", tmp_path / "audit.jsonl"):
            q = PostingQueue()

        assert q._entries == []


# ---------------------------------------------------------------------------
# Scenario 13: Duplicate topic block
# ---------------------------------------------------------------------------

class TestDuplicateTopicBlock:
    def test_near_identical_title_blocked(self, tmp_path):
        titles_file = tmp_path / "posted_titles.json"
        titles_file.write_text("[]", encoding="utf-8")
        q = _bare_queue([_entry(title="Test story on global trade deal")])
        with patch("config.PUBLISHED_TITLES_PATH", titles_file):
            assert q._is_duplicate_topic("Test story on global trade deal") is True

    def test_unrelated_title_not_blocked(self, tmp_path):
        titles_file = tmp_path / "posted_titles.json"
        titles_file.write_text("[]", encoding="utf-8")
        q = _bare_queue([_entry(title="Test story on global trade deal")])
        with patch("config.PUBLISHED_TITLES_PATH", titles_file):
            assert q._is_duplicate_topic("Earthquake strikes remote Pacific island") is False


# ---------------------------------------------------------------------------
# Scenario 14: Score below floor → REJECT
# ---------------------------------------------------------------------------

class TestScoreFloor:
    def test_score_below_65_is_rejected(self):
        assert route_story(64.9, 2) == Route.REJECT

    def test_score_at_65_is_scheduled(self):
        assert route_story(65.0, 2) == Route.SCHEDULE

    def test_score_at_80_is_publish_now(self):
        assert route_story(80.0, 2) == Route.PUBLISH_NOW

    def test_impact_10_overrides_low_score(self):
        assert route_story(50.0, 2, impact_score=10.0) == Route.PUBLISH_NOW


# ---------------------------------------------------------------------------
# Scenario 15: Unverified story → REVIEW_REQUIRED
# ---------------------------------------------------------------------------

class TestUnverifiedStory:
    def test_unverified_not_publishable(self):
        e = _entry(verification=VerificationStatus.UNVERIFIED.value)
        assert not has_publishable_work(_queue([e]))

    def test_speculative_status_not_publishable(self):
        e = _entry(status="REVIEW_REQUIRED", verification=VerificationStatus.SPECULATIVE.value)
        assert not has_publishable_work(_queue([e]))


# ---------------------------------------------------------------------------
# Scenario 16: Reconciliation — Tier 1 demoted to Tier 2
# ---------------------------------------------------------------------------

class TestReconciliation:
    def test_tier1_demoted_to_schedule_on_lower_score(self, tmp_path):
        from models import Story

        queue_file = tmp_path / "posting_queue.json"
        audit_file = tmp_path / "audit.jsonl"

        with patch("pipeline.posting_queue.QUEUE_FILE", queue_file), \
             patch("pipeline.posting_queue.AUDIT_FILE", audit_file):
            q = PostingQueue()
            e = _entry(route=Route.PUBLISH_NOW.value, score=85.0)
            q._entries = [e]

            story = MagicMock(spec=Story)
            story.source_url = e.source_url
            story.title = e.title
            story.verification_status = VerificationStatus.VERIFIED
            story.verification_score = 70.0
            story.verification_reason = "Re-evaluated"
            story.published_at = None

            result = q.reconcile_candidate(story, score=70.0, impact_score=0.0)

        assert result is True
        assert e.route == Route.SCHEDULE.value
        assert e.is_breaking is False


# ---------------------------------------------------------------------------
# Scenario 17: mark_retry exponential backoff
# ---------------------------------------------------------------------------

class TestMarkRetryBackoff:
    def test_first_retry_is_10_minutes(self, tmp_path):
        q = _bare_queue([_entry()])
        e = q._entries[0]
        with patch("pipeline.posting_queue.QUEUE_FILE", tmp_path / "q.json"), \
             patch("pipeline.posting_queue.AUDIT_FILE", tmp_path / "a.jsonl"):
            q.mark_retry(e, reason="Network error")

        delay = (
            datetime.fromisoformat(e.next_retry_at) - datetime.now(timezone.utc)
        ).total_seconds() / 60
        assert 8 <= delay <= 12

    def test_second_retry_is_20_minutes(self, tmp_path):
        e = _entry()
        e.publish_attempts = 1
        q = _bare_queue([e])
        with patch("pipeline.posting_queue.QUEUE_FILE", tmp_path / "q.json"), \
             patch("pipeline.posting_queue.AUDIT_FILE", tmp_path / "a.jsonl"):
            q.mark_retry(e, reason="Error again")

        delay = (
            datetime.fromisoformat(e.next_retry_at) - datetime.now(timezone.utc)
        ).total_seconds() / 60
        assert 18 <= delay <= 22

    def test_delay_caps_at_120_minutes(self, tmp_path):
        e = _entry()
        e.publish_attempts = 10
        q = _bare_queue([e])
        with patch("pipeline.posting_queue.QUEUE_FILE", tmp_path / "q.json"), \
             patch("pipeline.posting_queue.AUDIT_FILE", tmp_path / "a.jsonl"):
            q.mark_retry(e, reason="Persistent error")

        delay = (
            datetime.fromisoformat(e.next_retry_at) - datetime.now(timezone.utc)
        ).total_seconds() / 60
        assert delay <= 122


# ---------------------------------------------------------------------------
# Scenario 18: Worker propagates exit code
# ---------------------------------------------------------------------------

class TestWorkerExitCode:
    def test_propagates_exit_1(self):
        run = Mock(return_value=SimpleNamespace(returncode=1))
        with patch("publisher_worker.PostingQueue", return_value=_queue([_entry()])):
            assert run_worker(run=run) == 1

    def test_propagates_exit_0(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))
        with patch("publisher_worker.PostingQueue", return_value=_queue([_entry()])):
            assert run_worker(run=run) == 0


# ---------------------------------------------------------------------------
# Scenario 19: Single agent cycle per run_worker call
# ---------------------------------------------------------------------------

class TestSingleCyclePerRun:
    def test_agent_called_exactly_once_even_with_multiple_queued(self):
        run = Mock(return_value=SimpleNamespace(returncode=0))
        e1 = _entry(source_url="https://bbc.com/s1", title="Story 1")
        e2 = _entry(source_url="https://reuters.com/s2", title="Story 2")
        with patch("publisher_worker.PostingQueue", return_value=_queue([e1, e2])):
            run_worker(run=run)
        run.assert_called_once()
