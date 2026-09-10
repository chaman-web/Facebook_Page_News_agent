"""
pipeline/posting_queue.py — Editorial posting queue for Global Pulse News.

Two-tier behavior:
  Tier 1: score >= 80 or impact >= 10. Unlimited per day, with a 30-minute
          minimum gap from the previous post.
  Tier 2: score 65-79.9. Highest score publishes first, with a 30-minute gap
          and a limit of 12 regular posts per local day.
  Both tiers retain fresh queued stories for up to 48 hours. Temporary
  Facebook delivery failures remain queued for the next worker run.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from enum import Enum
from pathlib import Path
from typing import Optional

import config
from models import Story, VerificationStatus
from pipeline.deduplicator import canonical_story_url
from pipeline.editorial_scorer import CATEGORY_TIERS

logger = logging.getLogger(__name__)

QUEUE_FILE   = config.POSTING_QUEUE_PATH
AUDIT_FILE   = config.POSTING_DECISIONS_PATH

# ---------------------------------------------------------------------------
# Scheduled posting windows (local time HH:MM) — must match setup_scheduler.ps1
# ---------------------------------------------------------------------------

SCHEDULED_WINDOWS = config.PUBLISH_WINDOWS_LOCAL

# How many minutes before/after a window is "close enough" to count as in-window
WINDOW_TOLERANCE_MINUTES = 15


# ---------------------------------------------------------------------------
# Score thresholds — the routing table
# ---------------------------------------------------------------------------

SCORE_PUBLISH_NOW = 80.0
SCORE_NEXT_SLOT   = 75.0  # Legacy TTL/minimum-floor support; not assigned to new entries.
SCORE_SCHEDULE    = 65.0
SCORE_HOLD        = 50.0  # Legacy compatibility; new entries below 65 are rejected.

TIER1_PUBLISH_NOW = 85.0   # Backwards-compatible alias; new routing uses 80.
TIER1_NEXT_SLOT   = 60.0   # Backwards-compatible alias; new entries use SCHEDULE.

# Intervals
PUBLISH_NOW_MIN_GAP = timedelta(minutes=30)   # Same global cadence as Tier 2
NEXT_SLOT_INTERVAL  = PUBLISH_NOW_MIN_GAP
SCHEDULE_INTERVAL   = PUBLISH_NOW_MIN_GAP

# Daily limit for regular Tier 2 posts
TIER2_DAILY_LIMIT = 12
# Compatibility alias used by the CLI and older integrations. It now applies
# only to regular Tier 2 posts; Tier 1 breaking posts are unlimited.
HARD_DAILY_CEILING = TIER2_DAILY_LIMIT

# Dead hours — Feature #7
DEAD_HOURS_START     = 0
DEAD_HOURS_END       = 6
DEAD_HOURS_MIN_SCORE = 85.0

# Per-tier daily slot caps — Feature #8
TIER_DAILY_SLOTS = {1: 8, 2: 4, 3: 2}

# Audience fatigue — Feature #9
MAX_CONSECUTIVE_SAME_TIER = 3

# Category diversity — Feature #10
CATEGORY_REPEAT_WINDOW_MINUTES = 90
CATEGORY_REPEAT_SCORE_PREMIUM  = 8.0

# Topic diminishing returns — Feature #11
TOPIC_REPEAT_WINDOW_HOURS = 6
TOPIC_REPEAT_SCORE_STEP   = 10.0
TOPIC_REPEAT_MAX_PENALTY  = 30.0

# Per-lane TTL — Feature #2
LANE_TTL: dict[str, timedelta] = {
    route: timedelta(hours=48)
    for route in ("PUBLISH_NOW", "NEXT_SLOT", "SCHEDULE", "HOLD")
}

# Backwards-compat aliases
PUBLISH_FLOOR         = SCORE_SCHEDULE
LOW_NEWS_FLOOR        = SCORE_HOLD
BREAKING_SCORE_CUTOFF = SCORE_PUBLISH_NOW
MIN_ROUTINE_INTERVAL  = SCHEDULE_INTERVAL


# ---------------------------------------------------------------------------
# Route enum
# ---------------------------------------------------------------------------

class Route(str, Enum):
    PUBLISH_NOW = "PUBLISH_NOW"
    NEXT_SLOT   = "NEXT_SLOT"
    SCHEDULE    = "SCHEDULE"
    HOLD        = "HOLD"
    REJECT      = "REJECT"

    @property
    def label(self) -> str:
        return {
            Route.PUBLISH_NOW: "🚨 PUBLISH NOW",
            Route.NEXT_SLOT:   "⚡ NEXT SLOT",
            Route.SCHEDULE:    "📅 SCHEDULE",
            Route.HOLD:        "🗂️  HOLD",
            Route.REJECT:      "🗑️  REJECT",
        }[self]

    @property
    def priority(self) -> int:
        return {
            Route.PUBLISH_NOW: 0,
            Route.NEXT_SLOT:   1,
            Route.SCHEDULE:    2,
            Route.HOLD:        3,
            Route.REJECT:      4,
        }[self]


def route_story(score: float, tier: int, impact_score: float = 0.0) -> Route:
    """
    Single point of truth for routing. Called once at queue-entry time.

    New-entry score thresholds:
        impact ≥ 10 or score ≥ 80  PUBLISH_NOW
        score 65–79.9              SCHEDULE
        < 65    REJECT

    ``tier`` is retained for API compatibility. Existing NEXT_SLOT and HOLD
    entries remain supported by the queue's expiry and migration paths.
    """
    # High value goes now, moderate enters the score-ordered queue, and low
    # value never enters the queue.
    if impact_score >= 10 or score >= SCORE_PUBLISH_NOW:
        return Route.PUBLISH_NOW
    if score >= SCORE_SCHEDULE:           return Route.SCHEDULE
    return Route.REJECT


# ---------------------------------------------------------------------------
# Day type
# ---------------------------------------------------------------------------

class DayType(str):
    MAJOR_BREAKING = "MAJOR BREAKING DAY"
    BUSY           = "BUSY NEWS DAY"
    NORMAL         = "NORMAL DAY"
    LOW_NEWS       = "LOW NEWS DAY"


# ---------------------------------------------------------------------------
# QueueEntry
# ---------------------------------------------------------------------------

@dataclass
class QueueEntry:
    title:          str
    source_name:    str
    source_url:     str
    category:       str
    score:          float
    queued_at:      str
    is_breaking:    bool          = False
    category_tier:  int           = 2
    route:          str           = Route.SCHEDULE.value
    published_at:   Optional[str] = None
    status:         str           = "QUEUED"   # QUEUED | REVIEW_REQUIRED | PUBLISHED | SKIPPED | EXPIRED
    downgraded_from: Optional[str] = None      # original lane before TTL downgrade
    image_path:     Optional[str] = None       # pre-rendered image card path (set before queue)
    image_provenance: str         = ""
    image_credit:     str         = ""
    image_is_synthetic: bool      = False
    post_content:   Optional[str] = None       # pre-generated Facebook post copy
    card_headline:  Optional[str] = None       # AI-generated punchy card headline
    card_description: Optional[str] = None     # distinct supporting line rendered on the card
    hashtags:       Optional[list] = None      # pre-generated hashtags
    verification_status: str       = VerificationStatus.UNVERIFIED.value
    verification_score: float      = 0.0
    verification_reason: str       = ""
    impact_score:       float       = 0.0
    impact_reasons:     Optional[list] = None
    routing_reason:     str         = ""
    policy_decision:    str         = ""
    policy_categories:  Optional[list] = None
    policy_reasons:     Optional[list] = None
    policy_version:     str         = ""
    facebook_post_id: Optional[str] = None
    source_published_at: Optional[str] = None
    publish_attempts: int = 0
    last_publish_error: str = ""
    next_retry_at: Optional[str] = None

    @property
    def route_enum(self) -> Route:
        try:
            return Route(self.route)
        except ValueError:
            return Route.SCHEDULE

    def age(self, now: Optional[datetime] = None) -> timedelta:
        now = now or datetime.now(timezone.utc)
        return now - datetime.fromisoformat(self.source_published_at or self.queued_at)


# ---------------------------------------------------------------------------
# PostingQueue
# ---------------------------------------------------------------------------

class PostingQueue:
    """
    Editorial posting queue with two-tier routing, freshness expiry, audit
    logging, global score order, and minimum publishing intervals.
    """

    def __init__(self) -> None:
        self._entries: list[QueueEntry] = []
        self._load()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def add(
        self,
        story: Story,
        score: float,
        effective_tier: int | None = None,
        image_path: Optional[str] = None,
        post_content: Optional[str] = None,
        card_headline: Optional[str] = None,
        hashtags: Optional[list] = None,
        impact_score: float = 0.0,
        impact_reasons: Optional[list] = None,
    ) -> None:
        """Route a story, refreshing an existing queued candidate when found."""
        cat      = getattr(story, "category", "breaking")
        tier_num = effective_tier if effective_tier is not None else CATEGORY_TIERS.get(cat, 2)
        lane     = route_story(score, tier_num, impact_score)
        routing_reason = (
            "impact score >= 10"
            if impact_score >= 10
            else "editorial score >= 80"
            if score >= SCORE_PUBLISH_NOW
            else "editorial score >= 65"
        )

        # Re-scored queued stories retain their original queue time but receive
        # the latest evidence, content, score, and route. This permits an
        # automatic Tier 2 -> Tier 1 promotion after new corroboration.
        existing = next(
            (entry for entry in self._entries
             if canonical_story_url(entry.source_url) == canonical_story_url(story.source_url)
             and entry.status in {"QUEUED", "REVIEW_REQUIRED"}),
            None,
        )
        if existing is not None:
            previous_route = existing.route
            existing.title = story.title
            existing.source_name = story.source_name
            existing.category = cat
            existing.score = score
            existing.category_tier = tier_num
            existing.route = lane.value
            existing.is_breaking = lane == Route.PUBLISH_NOW
            existing.source_published_at = story.published_at.isoformat() if story.published_at else existing.source_published_at
            existing.image_path = str(image_path) if image_path else existing.image_path
            existing.image_provenance = getattr(story, "image_provenance", "") or existing.image_provenance
            existing.image_credit = getattr(story, "image_credit", "") or existing.image_credit
            existing.image_is_synthetic = bool(getattr(story, "image_is_synthetic", existing.image_is_synthetic))
            existing.post_content = post_content or existing.post_content
            existing.card_headline = card_headline or existing.card_headline
            existing.card_description = getattr(story, "card_description", None) or existing.card_description
            existing.hashtags = hashtags or existing.hashtags
            existing.verification_status = story.verification_status.value
            existing.verification_score = float(story.verification_score or 0.0)
            existing.verification_reason = story.verification_reason or ""
            existing.impact_score = float(impact_score or 0.0)
            existing.impact_reasons = list(impact_reasons or [])
            existing.routing_reason = routing_reason
            existing.status = "QUEUED" if story.verification_status == VerificationStatus.VERIFIED else "REVIEW_REQUIRED"
            self._save()
            self._audit(story.title, score, tier_num, lane, "REFRESHED",
                        f"Queue evidence refreshed; route {previous_route} -> {lane.value}")
            logger.info("Queue refreshed [%s -> %s | %.1f]: %s", previous_route, lane.value, score, story.title[:60])
            return

        # Similar topic already queued or published — block unchanged repeats.
        if self._is_duplicate_topic(story.title):
            logger.debug("🗑️  Duplicate topic blocked from queue: %s", story.title[:60])
            return

        if lane == Route.REJECT:
            self._audit(story.title, score, tier_num, lane, "REJECT", "Score below reject floor")
            logger.debug("🗑️  Rejected [%.1f T%d]: %s", score, tier_num, story.title[:60])
            return

        entry = QueueEntry(
            title         = story.title,
            source_name   = story.source_name,
            source_url    = story.source_url,
            category      = cat,
            score         = score,
            queued_at     = datetime.now(timezone.utc).isoformat(),
            source_published_at = story.published_at.isoformat() if story.published_at else None,
            is_breaking   = lane == Route.PUBLISH_NOW,
            category_tier = tier_num,
            route         = lane.value,
            image_path    = str(image_path) if image_path else None,
            image_provenance = getattr(story, "image_provenance", ""),
            image_credit = getattr(story, "image_credit", ""),
            image_is_synthetic = bool(getattr(story, "image_is_synthetic", False)),
            post_content  = post_content,
            card_headline = card_headline,
            card_description = getattr(story, "card_description", None),
            hashtags      = hashtags,
            verification_status = story.verification_status.value,
            verification_score  = float(story.verification_score or 0.0),
            verification_reason = story.verification_reason or "",
            impact_score = float(impact_score or 0.0),
            impact_reasons = list(impact_reasons or []),
            routing_reason = routing_reason,
            policy_decision = getattr(story, "policy_decision", ""),
            policy_categories = list(getattr(story, "policy_categories", []) or []),
            policy_reasons = list(getattr(story, "policy_reasons", []) or []),
            policy_version = getattr(story, "policy_version", ""),
            status = (
                "QUEUED"
                if story.verification_status == VerificationStatus.VERIFIED
                else "REVIEW_REQUIRED"
            ),
        )
        self._entries.append(entry)
        self._save()
        action = "QUEUED" if entry.status == "QUEUED" else "REVIEW_REQUIRED"
        detail = (
            f"Routed to {lane.value}: {routing_reason}"
            if entry.status == "QUEUED"
            else story.verification_reason or "Independent verification required"
        )
        self._audit(story.title, score, tier_num, lane, action, detail)
        logger.debug("%s [%s %.1f T%d]: %s", lane.label, action, score, tier_num, story.title[:55])

    def deserves_publishing(
        self,
        entry: QueueEntry,
        last_published_at: Optional[datetime],
        last_breaking_at:  Optional[datetime] = None,
    ) -> tuple[bool, str]:
        """Apply the two-tier timing, priority and daily-limit rules."""
        now = datetime.now(timezone.utc)
        lane = entry.route_enum
        if entry.next_retry_at:
            retry_at = datetime.fromisoformat(entry.next_retry_at)
            if retry_at > now:
                minutes = max(1, int((retry_at - now).total_seconds() / 60))
                return False, f"Publish retry backoff — {minutes}m remaining"
        ttl_result = self._check_ttl(entry, now)
        if ttl_result is not None:
            return ttl_result

        if lane == Route.PUBLISH_NOW:
            previous = last_published_at or self.last_published_at()
            if previous and now - previous < PUBLISH_NOW_MIN_GAP:
                remaining = PUBLISH_NOW_MIN_GAP - (now - previous)
                return False, f"Tier 1 cooldown — {max(1, int(remaining.total_seconds() / 60))}m remaining"
            return True, f"Tier 1 publish now [{entry.score:.1f}]"

        if self.has_actionable_tier1(now):
            return False, "Tier 1 priority — waiting for eligible high-impact stories"

        if entry.score < SCORE_SCHEDULE:
            return False, f"Score {entry.score:.1f} is below Tier 2 floor {SCORE_SCHEDULE:.0f}"
        if self.tier2_published_count() >= TIER2_DAILY_LIMIT:
            return False, f"Tier 2 daily limit ({self.tier2_published_count()}/{TIER2_DAILY_LIMIT})"

        previous = last_published_at or self.last_published_at()
        if previous and now - previous < NEXT_SLOT_INTERVAL:
            remaining = NEXT_SLOT_INTERVAL - (now - previous)
            return False, f"Tier 2 interval — {max(1, int(remaining.total_seconds() / 60))}m remaining"
        if not self._is_top_schedule_story(entry):
            return False, "Waiting behind a higher-scoring Tier 2 story"
        return True, f"Tier 2 next slot [{entry.score:.1f}]"

    def has_actionable_tier1(self, now: Optional[datetime] = None) -> bool:
        """Return whether a fresh Tier 1 entry is ready for this worker run."""
        now = now or datetime.now(timezone.utc)
        ttl = LANE_TTL[Route.PUBLISH_NOW.value]
        for candidate in self._entries:
            if candidate.status != "QUEUED" or candidate.route_enum != Route.PUBLISH_NOW:
                continue
            try:
                if candidate.age(now) > ttl:
                    continue
            except (TypeError, ValueError):
                # Invalid legacy timestamps require normal publisher review;
                # do not allow Tier 2 to jump ahead of them.
                return True
            if candidate.next_retry_at:
                try:
                    if datetime.fromisoformat(candidate.next_retry_at) > now:
                        continue
                except ValueError:
                    # A malformed retry timestamp must not silently remove a
                    # high-impact story from priority handling.
                    return True
            return True
        return False

    def next_publishable(
        self,
        last_published_at: Optional[datetime],
        last_breaking_at:  Optional[datetime] = None,
    ) -> Optional[QueueEntry]:
        """Return the highest-priority entry that deserves publishing right now."""
        queued = [e for e in self._entries if e.status == "QUEUED"]
        if not queued:
            return None
        queued.sort(key=self.priority_key)
        for entry in queued:
            ok, reason = self.deserves_publishing(entry, last_published_at, last_breaking_at)
            if ok:
                return entry
            logger.debug("⏸ [%s] %s | %s", entry.route, entry.title[:45], reason)
        return None

    def expire_stale(self) -> int:
        """
        Run TTL checks on all QUEUED entries and expire or downgrade them.
        Returns the number of entries that were expired or downgraded.
        """
        now     = datetime.now(timezone.utc)
        changed = 0
        for entry in self._entries:
            if entry.status != "QUEUED":
                continue
            if entry.route_enum != Route.PUBLISH_NOW and entry.score < SCORE_SCHEDULE:
                entry.status = "EXPIRED"
                self._audit(entry.title, entry.score, entry.category_tier,
                            entry.route_enum, "EXPIRED", "Below Tier 2 floor of 65")
                changed += 1
                continue
            entry_changed = False
            # A very old PUBLISH_NOW entry may need more than one transition
            # (PUBLISH_NOW → NEXT_SLOT → SCHEDULE → EXPIRED) in a single run.
            while entry.status == "QUEUED":
                before = (entry.status, entry.route, entry.downgraded_from, entry.is_breaking)
                self._check_ttl(entry, now)
                after = (entry.status, entry.route, entry.downgraded_from, entry.is_breaking)
                if after == before:
                    break
                entry_changed = True
            if entry_changed:
                changed += 1
        if changed:
            self._save()
        return changed

    def mark_published(
        self,
        entry: QueueEntry,
        post_id: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> None:
        entry.status       = "PUBLISHED"
        entry.published_at = published_at or datetime.now(timezone.utc).isoformat()
        entry.facebook_post_id = post_id
        entry.next_retry_at = None
        entry.last_publish_error = ""
        self._audit(
            entry.title, entry.score, entry.category_tier,
            entry.route_enum, "PUBLISHED",
            f"Published to Facebook [{entry.route}]",
        )
        self._save()

    def record_direct_publish(
        self,
        story: Story,
        score: float,
        effective_tier: int,
        image_path: Path,
        post_id: str,
        impact_score: float = 0.0,
        impact_reasons: Optional[list] = None,
    ) -> None:
        """Record an immediate post as history without first queueing it."""
        entry = next(
            (item for item in self._entries
             if canonical_story_url(item.source_url) == canonical_story_url(story.source_url)),
            None,
        )
        if entry is None:
            entry = QueueEntry(
                title=story.title,
                source_name=story.source_name,
                source_url=story.source_url,
                category=story.category,
                score=score,
                queued_at=datetime.now(timezone.utc).isoformat(),
                source_published_at=story.published_at.isoformat() if story.published_at else None,
                is_breaking=True,
                category_tier=effective_tier,
                route=Route.PUBLISH_NOW.value,
                image_path=str(image_path),
                image_provenance=getattr(story, "image_provenance", ""),
                image_credit=getattr(story, "image_credit", ""),
                image_is_synthetic=bool(getattr(story, "image_is_synthetic", False)),
                post_content=story.post_content,
                card_headline=story.card_headline,
                card_description=story.card_description,
                hashtags=story.hashtags,
                verification_status=story.verification_status.value,
                verification_score=float(story.verification_score or 0.0),
                verification_reason=story.verification_reason or "",
                impact_score=float(impact_score or 0.0),
                impact_reasons=list(impact_reasons or []),
                routing_reason=(
                    "impact score >= 10" if impact_score >= 10 else "editorial score >= 80"
                ),
                policy_decision=getattr(story, "policy_decision", ""),
                policy_categories=list(getattr(story, "policy_categories", []) or []),
                policy_reasons=list(getattr(story, "policy_reasons", []) or []),
                policy_version=getattr(story, "policy_version", ""),
            )
            self._entries.append(entry)
        else:
            entry.title = story.title
            entry.source_name = story.source_name
            entry.category = story.category
            entry.score = score
            entry.category_tier = effective_tier
            entry.route = Route.PUBLISH_NOW.value
            entry.is_breaking = True
            entry.source_published_at = story.published_at.isoformat() if story.published_at else entry.source_published_at
            entry.image_path = str(image_path)
            entry.image_provenance = getattr(story, "image_provenance", "")
            entry.image_credit = getattr(story, "image_credit", "")
            entry.image_is_synthetic = bool(getattr(story, "image_is_synthetic", False))
            entry.post_content = story.post_content
            entry.card_headline = story.card_headline
            entry.card_description = story.card_description
            entry.hashtags = story.hashtags
            entry.verification_status = story.verification_status.value
            entry.verification_score = float(story.verification_score or 0.0)
            entry.verification_reason = story.verification_reason or ""
            entry.impact_score = float(impact_score or 0.0)
            entry.impact_reasons = list(impact_reasons or [])
            entry.routing_reason = "impact score >= 10" if impact_score >= 10 else "editorial score >= 80"
        self.mark_published(entry, post_id=post_id)

    def mark_skipped(self, entry: QueueEntry, reason: str = "") -> None:
        entry.status = "SKIPPED"
        self._audit(entry.title, entry.score, entry.category_tier,
                    entry.route_enum, "SKIPPED", reason)
        if reason:
            logger.debug("Skipped (%s): %s", reason, entry.title[:50])
        self._save()

    def mark_retry(self, entry: QueueEntry, reason: str = "") -> None:
        """Preserve a publishable story after a temporary delivery failure."""
        entry.status = "QUEUED"
        entry.publish_attempts += 1
        entry.last_publish_error = reason
        delay_minutes = min(10 * (2 ** (entry.publish_attempts - 1)), 120)
        entry.next_retry_at = (
            datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        ).isoformat()
        self._audit(entry.title, entry.score, entry.category_tier,
                    entry.route_enum, "RETRY", f"{reason}; retry in {delay_minutes}m")
        self._save()

    def reconcile_candidate(
        self,
        story: Story,
        score: float,
        impact_score: float = 0.0,
        impact_reasons: Optional[list] = None,
    ) -> bool:
        """Safely demote or hold a queued candidate when fresh scoring weakens it."""
        entry = next((
            e for e in self._entries
            if canonical_story_url(e.source_url) == canonical_story_url(story.source_url)
            and e.status == "QUEUED"
        ), None)
        if entry is None:
            return False
        previous_route = entry.route
        desired = route_story(score, entry.category_tier, impact_score)
        entry.score = score
        entry.impact_score = float(impact_score or 0.0)
        entry.impact_reasons = list(impact_reasons or [])
        entry.verification_status = story.verification_status.value
        entry.verification_score = float(story.verification_score or 0.0)
        entry.verification_reason = story.verification_reason or ""
        decision = "RECONCILED"
        if story.verification_status != VerificationStatus.VERIFIED:
            entry.status = "REVIEW_REQUIRED"
            decision = "REVIEW_REQUIRED"
        elif desired == Route.REJECT:
            entry.status = "EXPIRED"
            decision = "DEMOTED_BELOW_FLOOR"
        elif entry.route_enum == Route.PUBLISH_NOW and desired != Route.PUBLISH_NOW:
            entry.route = Route.SCHEDULE.value
            entry.is_breaking = False
            decision = "DEMOTED_TO_TIER_2"
        self._audit(entry.title, score, entry.category_tier, entry.route_enum, decision,
                    f"Fresh fetch reconciliation: {previous_route} -> {entry.route}; score={score:.1f}")
        self._save()
        return True

    def effective_tier2_score(self, entry: QueueEntry, now: Optional[datetime] = None) -> float:
        """Use a small bounded age bonus so near-equal Tier 2 stories do not starve."""
        age_hours = max(0.0, entry.age(now).total_seconds() / 3600)
        return entry.score + min(age_hours / 12.0, 2.0)

    def priority_key(self, entry: QueueEntry) -> tuple:
        """Stable ordering for both publishing tiers."""
        if entry.route_enum == Route.PUBLISH_NOW:
            try:
                freshness = -datetime.fromisoformat(entry.source_published_at or entry.queued_at).timestamp()
            except (TypeError, ValueError):
                freshness = 0.0
            return (0, -entry.impact_score, freshness, -entry.verification_score, -entry.score, entry.queued_at)
        return (1, -self.effective_tier2_score(entry), -entry.score, entry.queued_at)

    def mark_review_required(self, entry: QueueEntry, reason: str = "") -> None:
        """Remove an unverified legacy entry from automatic publishing."""
        entry.status = "REVIEW_REQUIRED"
        entry.verification_reason = reason or entry.verification_reason
        self._audit(
            entry.title, entry.score, entry.category_tier,
            entry.route_enum, "REVIEW_REQUIRED", entry.verification_reason,
        )
        self._save()

    def daily_published_count(self) -> int:
        today = datetime.now().astimezone().date()
        return sum(
            1 for e in self._entries
            if e.status == "PUBLISHED" and e.published_at
            and datetime.fromisoformat(e.published_at).astimezone().date() == today
        )

    def can_publish_today(self) -> bool:
        return self.tier2_published_count() < TIER2_DAILY_LIMIT

    def tier1_published_count(self) -> int:
        today = datetime.now().astimezone().date()
        return sum(1 for e in self._entries if e.status == "PUBLISHED" and e.published_at
                   and e.route_enum == Route.PUBLISH_NOW
                   and datetime.fromisoformat(e.published_at).astimezone().date() == today)

    def tier2_published_count(self) -> int:
        return self.daily_published_count() - self.tier1_published_count()

    def last_published_at(self) -> Optional[datetime]:
        times = [datetime.fromisoformat(e.published_at) for e in self._entries
                 if e.status == "PUBLISHED" and e.published_at]
        return max(times) if times else None

    def queued_count(self) -> int:
        return sum(1 for e in self._entries if e.status == "QUEUED")

    def queued_by_lane(self) -> dict[str, int]:
        counts: dict[str, int] = {r.value: 0 for r in Route if r != Route.REJECT}
        for e in self._entries:
            if e.status == "QUEUED":
                counts[e.route] = counts.get(e.route, 0) + 1
        return counts

    def last_breaking_published_at(self) -> Optional[datetime]:
        """Return the time the most recent PUBLISH_NOW story was published today."""
        today = datetime.now(timezone.utc).date()
        breaking = [
            e for e in self._entries
            if e.status == "PUBLISHED"
            and e.published_at
            and (e.route == Route.PUBLISH_NOW.value or e.is_breaking)
            and datetime.fromisoformat(e.published_at).date() == today
        ]
        if not breaking:
            return None
        breaking.sort(key=lambda e: e.published_at or "", reverse=True)
        return datetime.fromisoformat(breaking[0].published_at)

    def purge_old(self, max_age_hours: int = 48) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        before = len(self._entries)
        self._entries = [
            e for e in self._entries
            if not (
                e.status in ("QUEUED", "EXPIRED")
                and datetime.fromisoformat(e.queued_at) < cutoff
            )
        ]
        removed = before - len(self._entries)
        if removed:
            logger.info("Purged %d old queue entries.", removed)
            self._save()

    def summary(self) -> str:
        lanes     = self.queued_by_lane()
        return (
            f"{self.queued_count()} queued "
            f"[Tier 1:{lanes['PUBLISH_NOW']} Tier 2:{self.queued_count() - lanes['PUBLISH_NOW']}] | "
            f"published today [Tier 1:{self.tier1_published_count()} unlimited "
            f"Tier 2:{self.tier2_published_count()}/{TIER2_DAILY_LIMIT}]"
        )

    # ------------------------------------------------------------------
    # Day classification
    # ------------------------------------------------------------------

    def _classify_day(self) -> str:
        queued = [e for e in self._entries if e.status == "QUEUED"]
        if not queued:
            return DayType.LOW_NEWS
        high  = sum(1 for e in queued if e.score >= 85)
        strong= sum(1 for e in queued if e.score >= 70)
        t1    = sum(1 for e in queued if e.category_tier == 1)
        if high >= 3 or (t1 >= 4 and high >= 1):
            return DayType.MAJOR_BREAKING
        if strong >= 6:
            return DayType.BUSY
        if strong >= 3:
            return DayType.NORMAL
        return DayType.LOW_NEWS

    # ------------------------------------------------------------------
    # Feature #1 — Scheduled window awareness
    # ------------------------------------------------------------------

    def _in_scheduled_window(self, now: datetime) -> tuple[bool, str]:
        """
        Returns (True, window_label) if current local time is within
        WINDOW_TOLERANCE_MINUTES of any scheduled window.
        Returns (False, "") otherwise.
        """
        local_now = datetime.now()   # local time
        for window_str in SCHEDULED_WINDOWS:
            wh, wm = map(int, window_str.split(":"))
            window_dt = local_now.replace(hour=wh, minute=wm, second=0, microsecond=0)
            delta_mins = abs((local_now - window_dt).total_seconds() / 60)
            if delta_mins <= WINDOW_TOLERANCE_MINUTES:
                return True, window_str
        return False, ""

    def _next_window_label(self, now: datetime) -> str:
        """Return the label of the next upcoming scheduled window."""
        local_now = datetime.now()
        upcoming = []
        for window_str in SCHEDULED_WINDOWS:
            wh, wm = map(int, window_str.split(":"))
            window_dt = local_now.replace(hour=wh, minute=wm, second=0, microsecond=0)
            if window_dt > local_now:
                upcoming.append(window_str)
        if upcoming:
            return upcoming[0]
        return SCHEDULED_WINDOWS[0] + " (tomorrow)"

    # ------------------------------------------------------------------
    # Feature #2, #3 — TTL expiry and re-routing
    # ------------------------------------------------------------------

    def _check_ttl(self, entry: QueueEntry, now: datetime) -> Optional[tuple[bool, str]]:
        """Expire a queued story 48 hours after its source publication time."""
        lane = entry.route_enum
        ttl  = LANE_TTL.get(lane.value)
        if ttl is None:
            return None

        age = entry.age(now)
        if age <= ttl:
            return None   # still fresh

        entry.status = "EXPIRED"
        reason = (
            f"Freshness expired: {lane.value} stories remain queued for "
            f"{int(ttl.total_seconds()/3600)}h (age: {age.total_seconds()/3600:.1f}h)"
        )
        logger.info("🗑️  Expired [%s]: %s", lane.value, entry.title[:55])
        self._audit(
            entry.title, entry.score, entry.category_tier,
            lane, "EXPIRED", reason,
        )
        return False, reason

    # ------------------------------------------------------------------
    # Duplicate topic detection
    # ------------------------------------------------------------------

    def _is_duplicate_topic(self, title: str) -> bool:
        """
        Returns True if this title covers the same event as a story already
        in the queue or previously published.
        Uses two signals:
          1. SequenceMatcher ratio >= 0.72 (near-identical wording)
          2. Keyword overlap >= 3 words AND >= 45% of title keywords match
        """
        from difflib import SequenceMatcher
        from pipeline.deduplicator import is_meaningful_update
        import json as _json
        from pathlib import Path as _Path

        _stop = {
            "the","a","an","and","or","but","in","on","at","to","for","of","with",
            "by","from","as","is","was","are","were","be","been","its","this","that",
            "says","said","new","more","than","up","out","after","before","about",
            "just","over","not","has","have","had","will","would","could","should",
        }

        def kw(text: str) -> set[str]:
            return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _stop}

        def norm(text: str) -> str:
            return re.sub(r"[^a-z0-9 ]", " ", text.lower()).strip()

        new_norm = norm(title)
        new_kw   = kw(title)

        # Collect all comparison titles: queued + published entries
        compare: list[str] = [e.title for e in self._entries
                               if e.status in ("QUEUED", "PUBLISHED")]
        try:
            pf = _Path(config.PUBLISHED_TITLES_PATH)
            if pf.exists():
                compare += _json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            pass

        for other in compare:
            if is_meaningful_update(title, other):
                continue
            # Signal 1: near-identical wording
            if SequenceMatcher(None, new_norm, norm(other)).ratio() >= 0.72:
                return True
            # Signal 2: same topic, different wording
            if len(new_kw) >= 4:
                overlap = new_kw & kw(other)
                if len(overlap) >= 3 and len(overlap) / len(new_kw) >= 0.45:
                    return True

        return False

    # ------------------------------------------------------------------
    # Feature #4 — Audit log
    # ------------------------------------------------------------------

    def _audit(
        self,
        title: str,
        score: float,
        tier: int,
        lane: Route,
        decision: str,
        reason: str,
    ) -> None:
        """Append one line to posting_decisions.jsonl."""
        record = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "title":    title[:80],
            "score":    round(score, 1),
            "tier":     tier,
            "lane":     lane.value if isinstance(lane, Route) else str(lane),
            "decision": decision,
            "reason":   reason[:120],
        }
        try:
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("Audit log write failed: %s", exc)

    # ------------------------------------------------------------------
    # Feature #5 — PUBLISH_NOW token bucket (handled in deserves_publishing)
    # Feature #6 — Slot reservation with per-tier category mix
    # ------------------------------------------------------------------

    def _is_top_schedule_story(self, entry: QueueEntry) -> bool:
        """Return whether entry is the globally highest-scoring regular story."""
        now = datetime.now(timezone.utc)
        regular = [
            e for e in self._entries
            if e.status == "QUEUED"
            and e.route_enum != Route.PUBLISH_NOW
            and e.score >= SCORE_SCHEDULE
            and (
                not e.next_retry_at
                or datetime.fromisoformat(e.next_retry_at) <= now
            )
        ]
        if not regular:
            return True
        best = min(regular, key=self.priority_key)
        return entry.source_url == best.source_url

    def window_slot_picks(self) -> list[QueueEntry]:
        """
        Return the up-to-3 stories that would publish in the next window:
        one per tier, each the highest-scoring in its tier.
        Useful for dry-run previews and scheduler logging.
        """
        schedule_queued = [
            e for e in self._entries
            if e.status == "QUEUED" and e.route == Route.SCHEDULE.value
        ]
        picks = []
        for tier in (1, 2, 3):
            tier_stories = [e for e in schedule_queued if e.category_tier == tier]
            if tier_stories:
                picks.append(max(tier_stories, key=lambda e: e.score))
        return picks

    # ------------------------------------------------------------------
    # Feature #7 — Dead hours
    # ------------------------------------------------------------------

    def is_dead_hours(self) -> bool:
        return DEAD_HOURS_START <= datetime.now().hour < DEAD_HOURS_END

    # ------------------------------------------------------------------
    # Feature #8 — Per-tier slot caps
    # ------------------------------------------------------------------

    def _tier_daily_count(self, tier: int) -> int:
        today = datetime.now(timezone.utc).date()
        return sum(
            1 for e in self._entries
            if e.status == "PUBLISHED"
            and e.category_tier == tier
            and e.published_at
            and datetime.fromisoformat(e.published_at).date() == today
        )

    # ------------------------------------------------------------------
    # Feature #9 — Audience fatigue
    # ------------------------------------------------------------------

    def _fatigued_tier(self) -> Optional[int]:
        today = datetime.now(timezone.utc).date()
        recent = sorted(
            [
                e for e in self._entries
                if e.status == "PUBLISHED"
                and e.published_at
                and datetime.fromisoformat(e.published_at).date() == today
            ],
            key=lambda e: e.published_at or "",
            reverse=True,
        )[:MAX_CONSECUTIVE_SAME_TIER]
        if len(recent) < MAX_CONSECUTIVE_SAME_TIER:
            return None
        tiers = [e.category_tier for e in recent]
        if len(set(tiers)) == 1 and tiers[0] != 1:
            return tiers[0]
        return None

    # ------------------------------------------------------------------
    # Feature #10 — Category diversity premium
    # ------------------------------------------------------------------

    def _category_repeat_premium(self, category: str, now: datetime) -> float:
        cutoff = now - timedelta(minutes=CATEGORY_REPEAT_WINDOW_MINUTES)
        same = [
            e for e in self._entries
            if e.status == "PUBLISHED"
            and e.category == category
            and e.published_at
            and datetime.fromisoformat(e.published_at) >= cutoff
        ]
        return CATEGORY_REPEAT_SCORE_PREMIUM if same else 0.0

    # ------------------------------------------------------------------
    # Feature #11 — Topic diminishing returns
    # ------------------------------------------------------------------

    def _topic_repeat_penalty(self, title: str, now: datetime) -> float:
        cutoff = now - timedelta(hours=TOPIC_REPEAT_WINDOW_HOURS)
        words  = _key_words(title)
        if not words:
            return 0.0
        count = 0
        for e in self._entries:
            if e.status != "PUBLISHED" or not e.published_at:
                continue
            if datetime.fromisoformat(e.published_at) < cutoff:
                continue
            overlap = len(words & _key_words(e.title)) / max(len(words), 1)
            if overlap >= 0.25:
                count += 1
        penalty = min(count * TOPIC_REPEAT_SCORE_STEP, TOPIC_REPEAT_MAX_PENALTY)
        if penalty:
            logger.debug("Topic repeat ×%d → +%.0f floor for '%s'", count, penalty, title[:40])
        return penalty

    # ------------------------------------------------------------------
    # Per-lane score floor
    # ------------------------------------------------------------------

    def _min_score_for(self, lane: Route, day_type: str) -> float:
        if lane == Route.PUBLISH_NOW: return 0.0
        if lane == Route.NEXT_SLOT:
            # On slow news days, lower the bar so good-enough stories still go out
            return SCORE_SCHEDULE if day_type == DayType.LOW_NEWS else SCORE_NEXT_SLOT
        if lane == Route.SCHEDULE:
            return LOW_NEWS_FLOOR if day_type == DayType.LOW_NEWS else SCORE_SCHEDULE
        if lane == Route.HOLD:        return SCORE_HOLD
        return SCORE_SCHEDULE

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not QUEUE_FILE.exists():
            return
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            # Support both formats: {"entries": [...]} and bare [...]
            if isinstance(data, dict):
                entries_raw = data.get("entries", [])
            else:
                entries_raw = data
            self._entries = []
            for e in entries_raw:
                legacy_image_name = Path(e.get("image_path") or "").name.lower()
                inferred_provenance = (
                    "branded_fallback" if legacy_image_name.endswith("_fallback.jpg")
                    else "legacy_unknown" if legacy_image_name
                    else ""
                )
                e.setdefault("category_tier",   CATEGORY_TIERS.get(e.get("category", "breaking"), 2))
                e.setdefault("route",            Route.SCHEDULE.value)
                e.setdefault("downgraded_from",  None)
                e.setdefault("image_path",       None)
                if not e.get("image_provenance"):
                    e["image_provenance"] = inferred_provenance
                if not e.get("image_credit") and inferred_provenance == "branded_fallback":
                    e["image_credit"] = "Global Pulse News"
                else:
                    e.setdefault("image_credit", "")
                e.setdefault("image_is_synthetic", False)
                e.setdefault("post_content",     None)
                e.setdefault("card_headline",    None)
                e.setdefault("card_description", None)
                e.setdefault("hashtags",         None)
                e.setdefault("verification_status", VerificationStatus.UNVERIFIED.value)
                e.setdefault("verification_score",  0.0)
                e.setdefault("impact_score",        0.0)
                e.setdefault("impact_reasons",      [])
                e.setdefault("routing_reason",      "legacy queue entry")
                e.setdefault("policy_decision",     "")
                e.setdefault("policy_categories",   [])
                e.setdefault("policy_reasons",      [])
                e.setdefault("policy_version",      "")
                e.setdefault("source_published_at", e.get("queued_at"))
                e.setdefault("publish_attempts", 0)
                e.setdefault("last_publish_error", "")
                e.setdefault("next_retry_at", None)
                e.setdefault(
                    "verification_reason",
                    "Legacy queue entry has no independent-source verification evidence.",
                )
                self._entries.append(QueueEntry(**e))
        except Exception as exc:
            logger.warning("Could not load posting queue (%s) — starting fresh.", exc)
            self._entries = []

    def _save(self) -> None:
        try:
            QUEUE_FILE.write_text(
                json.dumps(
                    {"entries": [e.__dict__ for e in self._entries]},
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Could not save posting queue: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "that", "with", "this", "from", "they", "their", "have", "been", "will",
    "were", "after", "over", "into", "about", "more", "also", "said", "says",
    "report", "update", "latest", "breaking", "world", "global", "news",
}


def _key_words(title: str) -> set[str]:
    tokens = re.findall(r"\b[a-zA-Z]{4,}\b", title.lower())
    return {t for t in tokens if t not in _STOPWORDS}
