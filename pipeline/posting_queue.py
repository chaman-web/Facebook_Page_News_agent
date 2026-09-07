"""
pipeline/posting_queue.py — Editorial posting queue for Global Pulse News.

Publishing philosophy:
  The queue never asks "have we posted enough today?"
  It only asks "does this story deserve our audience's attention right now?"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ROUTING LANES  (assigned at queue-entry time via route_story)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  🚨 PUBLISH_NOW   score ≥ 88  (Tier 1: ≥ 85)
     Bypass interval, dead-hours, fatigue. Publish immediately.
     TTL: 2 hours. After that, downgrades to NEXT_SLOT.
     Token-bucket: minimum 10-min gap between two PUBLISH_NOW posts.

  ⚡ NEXT_SLOT     score 75–87  (Tier 1: 60–84)
     Publish at next available slot. 30-min minimum gap.
     TTL: 6 hours. After that, downgrades to SCHEDULE.

  📅 SCHEDULE      score 60–74  (Tier 1: 50–59)
     Holds for the next scheduled window (07:30 / 12:30 / 19:30).
     Only the highest-scoring SCHEDULE story publishes per window slot.
     TTL: 12 hours.

  🗂️  HOLD          score 50–59
     Parked. Surfaces only on LOW_NEWS days. TTL: end of day.

  🗑️  REJECT        score < 50
     Dropped. Never enters the queue.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Day classification (auto-detected from queue content):
    MAJOR BREAKING  → publish as they arrive
    BUSY NEWS DAY   → normal interval, higher ceiling
    NORMAL DAY      → standard operation
    LOW NEWS DAY    → HOLD lane activated, floor drops to 50

  Hard safety ceiling: 15 posts/day (circuit breaker only).
  No minimum. Zero posts on a dead day is valid.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Features:
  #1  — Scheduled window awareness   (SCHEDULE lane waits for 07:30/12:30/19:30)
  #2  — Per-lane TTL + auto-expiry   (PUBLISH_NOW 2h, NEXT_SLOT 6h, SCHEDULE 12h, HOLD EoD)
  #3  — Re-routing on TTL expiry     (PUBLISH_NOW→NEXT_SLOT, NEXT_SLOT→SCHEDULE on age)
  #4  — Audit log                    (posting_decisions.jsonl — every decision recorded)
  #5  — PUBLISH_NOW token bucket     (min 10-min gap between consecutive breaking posts)
  #6  — Slot reservation             (only top-scoring SCHEDULE story takes each window slot)
  #7  — Dead hours suppression       (midnight–6 AM: only PUBLISH_NOW passes)
  #8  — Per-tier daily slot caps     (prevents category flooding)
  #9  — Audience fatigue             (no 3+ consecutive same-tier posts)
  #10 — Category diversity premium   (same-category repeat needs extra score)
  #11 — Topic diminishing returns    (same-topic repeats raise the bar)
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

from models import Story
from pipeline.editorial_scorer import CATEGORY_TIERS

logger = logging.getLogger(__name__)

QUEUE_FILE   = Path("posting_queue.json")
AUDIT_FILE   = Path("posting_decisions.jsonl")

# ---------------------------------------------------------------------------
# Scheduled posting windows (local time HH:MM) — must match scheduler.py
# ---------------------------------------------------------------------------

SCHEDULED_WINDOWS = ["08:00", "13:00", "19:00"]  # UTC — peak global engagement windows

# How many minutes before/after a window is "close enough" to count as in-window
WINDOW_TOLERANCE_MINUTES = 15


# ---------------------------------------------------------------------------
# Score thresholds — the routing table
# ---------------------------------------------------------------------------

SCORE_PUBLISH_NOW = 88.0
SCORE_NEXT_SLOT   = 75.0
SCORE_SCHEDULE    = 60.0
SCORE_HOLD        = 50.0
# < SCORE_HOLD → REJECT

TIER1_PUBLISH_NOW = 85.0   # Tier 1 earns PUBLISH_NOW at this score
TIER1_NEXT_SLOT   = 60.0   # Tier 1 earns NEXT_SLOT at this score

# Intervals
PUBLISH_NOW_MIN_GAP = timedelta(minutes=10)   # Feature #5 — token bucket
NEXT_SLOT_INTERVAL  = timedelta(minutes=30)
SCHEDULE_INTERVAL   = timedelta(hours=1)

# Hard daily ceiling — circuit breaker only
HARD_DAILY_CEILING = 15

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
    "PUBLISH_NOW": timedelta(hours=2),
    "NEXT_SLOT":   timedelta(hours=6),
    "SCHEDULE":    timedelta(hours=12),
    "HOLD":        timedelta(hours=18),   # effectively end of day
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


def route_story(score: float, tier: int) -> Route:
    """
    Single point of truth for routing. Called once at queue-entry time.

    Score thresholds:
        Score       Tier 1          Tier 2/3
        ─────────────────────────────────────
        ≥ 88        PUBLISH_NOW     PUBLISH_NOW
        85–87       PUBLISH_NOW     NEXT_SLOT
        75–84       NEXT_SLOT       NEXT_SLOT
        60–74       NEXT_SLOT       SCHEDULE
        50–59       SCHEDULE        HOLD
        < 50        REJECT          REJECT
    """
    if score >= SCORE_PUBLISH_NOW:
        return Route.PUBLISH_NOW
    if tier == 1:
        if score >= TIER1_PUBLISH_NOW:   return Route.PUBLISH_NOW
        if score >= TIER1_NEXT_SLOT:     return Route.NEXT_SLOT
        if score >= SCORE_HOLD:          return Route.SCHEDULE
        return Route.REJECT
    # Tier 2/3
    if score >= SCORE_NEXT_SLOT:         return Route.NEXT_SLOT
    if score >= SCORE_SCHEDULE:          return Route.SCHEDULE
    if score >= SCORE_HOLD:              return Route.HOLD
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
    status:         str           = "QUEUED"   # QUEUED | PUBLISHED | SKIPPED | EXPIRED
    downgraded_from: Optional[str] = None      # original lane before TTL downgrade
    image_path:     Optional[str] = None       # pre-rendered image card path (set before queue)
    post_content:   Optional[str] = None       # pre-generated Facebook post copy
    card_headline:  Optional[str] = None       # AI-generated punchy card headline
    hashtags:       Optional[list] = None      # pre-generated hashtags

    @property
    def route_enum(self) -> Route:
        try:
            return Route(self.route)
        except ValueError:
            return Route.SCHEDULE

    def age(self, now: Optional[datetime] = None) -> timedelta:
        now = now or datetime.now(timezone.utc)
        return now - datetime.fromisoformat(self.queued_at)


# ---------------------------------------------------------------------------
# PostingQueue
# ---------------------------------------------------------------------------

class PostingQueue:
    """
    Editorial posting queue with 4-lane routing, TTL expiry, audit log,
    scheduled window awareness, and token-bucket rate limiting.
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
    ) -> None:
        """Route and enqueue a story. Silently ignores duplicates."""
        # 1. Exact URL already in queue
        for e in self._entries:
            if e.source_url == story.source_url:
                return

        # 2. Similar topic already queued or published — block same-event duplicates
        if self._is_duplicate_topic(story.title):
            logger.debug("🗑️  Duplicate topic blocked from queue: %s", story.title[:60])
            return

        cat      = getattr(story, "category", "breaking")
        tier_num = effective_tier if effective_tier is not None else CATEGORY_TIERS.get(cat, 2)
        lane     = route_story(score, tier_num)

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
            is_breaking   = lane == Route.PUBLISH_NOW,
            category_tier = tier_num,
            route         = lane.value,
            image_path    = str(image_path) if image_path else None,
            post_content  = post_content,
            card_headline = card_headline,
            hashtags      = hashtags,
        )
        self._entries.append(entry)
        self._save()
        self._audit(story.title, score, tier_num, lane, "QUEUED", f"Routed to {lane.value}")
        logger.debug("%s [%.1f T%d]: %s", lane.label, score, tier_num, story.title[:55])

    def deserves_publishing(
        self,
        entry: QueueEntry,
        last_published_at: Optional[datetime],
        last_breaking_at:  Optional[datetime] = None,
    ) -> tuple[bool, str]:
        """
        The core editorial question: does this story deserve publishing right now?

        Checks (fail-fast):
          1.  Hard ceiling
          2.  TTL expiry / downgrade         — Feature #2, #3
          3.  Dead hours                     — Feature #7
          4.  Per-tier slot cap              — Feature #8
          5.  Audience fatigue               — Feature #9
          6.  Score floor + premiums         — Features #10, #11
          7.  HOLD surface gate              — low-news days only
          8.  PUBLISH_NOW token bucket       — Feature #5
          9.  SCHEDULE window awareness      — Feature #1, #6
          10. NEXT_SLOT interval
          11. SCHEDULE/HOLD interval
        """
        now   = datetime.now(timezone.utc)
        tier  = entry.category_tier
        score = entry.score
        lane  = entry.route_enum

        # 1. Hard ceiling
        if self.daily_published_count() >= HARD_DAILY_CEILING:
            return False, f"Hard safety ceiling ({self.daily_published_count()}/{HARD_DAILY_CEILING})"

        # 2. TTL expiry + re-routing (Feature #2, #3)
        ttl_result = self._check_ttl(entry, now)
        if ttl_result is not None:
            return ttl_result   # (False, reason) if expired; lane was mutated in-place if downgraded
        # Re-read lane after possible downgrade
        lane = entry.route_enum

        # 3. Dead hours — only PUBLISH_NOW passes
        if self.is_dead_hours() and lane != Route.PUBLISH_NOW:
            return False, (
                f"Dead hours ({DEAD_HOURS_START:02d}:00–{DEAD_HOURS_END:02d}:00) — "
                f"only 🚨 PUBLISH_NOW passes (need {DEAD_HOURS_MIN_SCORE:.0f}, have {score:.0f})"
            )

        # 4. Per-tier slot cap — PUBLISH_NOW bypasses cap (breaking news always goes through)
        tier_count = self._tier_daily_count(tier)
        if tier_count >= TIER_DAILY_SLOTS[tier] and lane != Route.PUBLISH_NOW:
            return False, f"Tier {tier} slot cap ({tier_count}/{TIER_DAILY_SLOTS[tier]})"

        # 5. Audience fatigue (Tier 1 exempt)
        fatigued = self._fatigued_tier()
        if tier != 1 and tier == fatigued:
            return False, (
                f"Audience fatigue — {MAX_CONSECUTIVE_SAME_TIER} consecutive "
                f"Tier {tier} posts. Forcing tier switch."
            )

        # 6. Score floor + category/topic premiums
        day_type  = self._classify_day()
        min_score = self._min_score_for(lane, day_type)

        cat_premium   = self._category_repeat_premium(entry.category, now)
        topic_penalty = self._topic_repeat_penalty(entry.title, now)
        min_score    += cat_premium + topic_penalty

        # South Asia regional exemption — Pakistan / India stories get a
        # 15-point floor reduction to ensure regional coverage is not
        # starved out by thin RSS summaries.
        SOUTH_ASIA_KEYWORDS = [
            "pakistan", "india", "islamabad", "karachi", "lahore", "delhi",
            "mumbai", "kashmir", "sindh", "punjab", "modi", "nawaz", "imran",
            "indian", "pakistani", "bangladesh", "karnataka",
        ]
        title_lower = entry.title.lower()
        if any(k in title_lower for k in SOUTH_ASIA_KEYWORDS):
            min_score = max(min_score - 15.0, SCORE_HOLD)

        if score < min_score:
            parts = [f"base={self._min_score_for(lane, day_type):.0f}"]
            if cat_premium:   parts.append(f"+cat={cat_premium:.0f}")
            if topic_penalty: parts.append(f"+topic={topic_penalty:.0f}")
            return False, (
                f"Score {score:.1f} < floor {min_score:.1f} "
                f"[{' '.join(parts)}] lane={lane.value} day={day_type}"
            )

        # 7. HOLD only surfaces on LOW_NEWS days
        if lane == Route.HOLD and day_type != DayType.LOW_NEWS:
            return False, f"HOLD surfaces only on LOW_NEWS days (today: {day_type})"

        # 8. PUBLISH_NOW token bucket (Feature #5)
        if lane == Route.PUBLISH_NOW:
            if last_breaking_at is not None:
                gap = now - last_breaking_at
                if gap < PUBLISH_NOW_MIN_GAP:
                    wait = int((PUBLISH_NOW_MIN_GAP - gap).total_seconds() / 60)
                    return False, f"🚨 Breaking cooldown — {wait}m remaining (10-min gap)"
            return True, f"🚨 PUBLISH NOW [{score:.1f}]"

        # 9. SCHEDULE window awareness + slot reservation (Feature #1, #6)
        if lane == Route.SCHEDULE:
            in_window, window_label = self._in_scheduled_window(now)
            if not in_window:
                next_w = self._next_window_label(now)
                return False, f"📅 Waiting for scheduled window (next: {next_w})"
            # Slot reservation: only the top-scoring SCHEDULE story takes this window
            if not self._is_top_schedule_story(entry):
                return False, f"📅 Slot taken by higher-scoring story in window {window_label}"
            return True, f"📅 SCHEDULED [{score:.1f}] window={window_label}"

        # 10. NEXT_SLOT — 30-min gap
        if lane == Route.NEXT_SLOT:
            if last_published_at is not None:
                gap = now - last_published_at
                if gap < NEXT_SLOT_INTERVAL:
                    wait = int((NEXT_SLOT_INTERVAL - gap).total_seconds() / 60)
                    return False, f"⚡ NEXT_SLOT — {wait}m remaining (30-min gap)"
            return True, f"⚡ NEXT SLOT [{score:.1f}]"

        # 11. HOLD — 1-hr gap (low-news day, already gated in step 7)
        if lane == Route.HOLD:
            if last_published_at is not None:
                gap = now - last_published_at
                if gap < SCHEDULE_INTERVAL:
                    wait = int((SCHEDULE_INTERVAL - gap).total_seconds() / 60)
                    return False, f"🗂️ HOLD — {wait}m remaining (1-hr gap)"
            return True, f"🗂️ HOLD surfaced [{score:.1f}] (low-news day)"

        return True, f"Clears editorial bar [{score:.1f}]"

    def next_publishable(
        self,
        last_published_at: Optional[datetime],
        last_breaking_at:  Optional[datetime] = None,
    ) -> Optional[QueueEntry]:
        """Return the highest-priority entry that deserves publishing right now."""
        queued = [e for e in self._entries if e.status == "QUEUED"]
        if not queued:
            return None
        queued.sort(key=lambda e: (e.route_enum.priority, -e.score))
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
            result = self._check_ttl(entry, now)
            if result is not None:
                changed += 1
        if changed:
            self._save()
        return changed

    def mark_published(self, entry: QueueEntry) -> None:
        entry.status       = "PUBLISHED"
        entry.published_at = datetime.now(timezone.utc).isoformat()
        self._audit(
            entry.title, entry.score, entry.category_tier,
            entry.route_enum, "PUBLISHED",
            f"Published to Facebook [{entry.route}]",
        )
        self._save()

    def mark_skipped(self, entry: QueueEntry, reason: str = "") -> None:
        entry.status = "SKIPPED"
        self._audit(entry.title, entry.score, entry.category_tier,
                    entry.route_enum, "SKIPPED", reason)
        if reason:
            logger.debug("Skipped (%s): %s", reason, entry.title[:50])
        self._save()

    def daily_published_count(self) -> int:
        today = datetime.now(timezone.utc).date()
        return sum(
            1 for e in self._entries
            if e.status == "PUBLISHED" and e.published_at
            and datetime.fromisoformat(e.published_at).date() == today
        )

    def can_publish_today(self) -> bool:
        return self.daily_published_count() < HARD_DAILY_CEILING

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
        day_type  = self._classify_day()
        published = self.daily_published_count()
        lanes     = self.queued_by_lane()
        t1 = self._tier_daily_count(1)
        t2 = self._tier_daily_count(2)
        t3 = self._tier_daily_count(3)
        fatigued  = self._fatigued_tier()
        now       = datetime.now(timezone.utc)
        in_win, win_label = self._in_scheduled_window(now)
        dead_note = " | 🌙 DEAD HOURS" if self.is_dead_hours() else ""
        win_note  = f" | ⏰ {win_label}" if in_win else f" | next: {self._next_window_label(now)}"
        fat_note  = f" | 😴 T{fatigued} fatigued" if fatigued else ""
        lane_str  = (
            f"🚨{lanes['PUBLISH_NOW']} ⚡{lanes['NEXT_SLOT']} "
            f"📅{lanes['SCHEDULE']} 🗂️{lanes['HOLD']}"
        )
        return (
            f"{self.queued_count()} queued [{lane_str}] | "
            f"{published} published [{day_type}] "
            f"[T1:{t1}/{TIER_DAILY_SLOTS[1]} T2:{t2}/{TIER_DAILY_SLOTS[2]} T3:{t3}/{TIER_DAILY_SLOTS[3]}]"
            f"{dead_note}{win_note}{fat_note}"
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
        """
        Check if this entry has exceeded its lane TTL.

        - PUBLISH_NOW expired → downgrade to NEXT_SLOT, return None (still publishable)
        - NEXT_SLOT expired   → downgrade to SCHEDULE, return None
        - SCHEDULE expired    → mark EXPIRED, return (False, reason)
        - HOLD expired        → mark EXPIRED, return (False, reason)

        Returns None if no TTL action was taken (entry is still valid in its lane).
        Returns (False, reason) if the entry was expired.
        """
        lane = entry.route_enum
        ttl  = LANE_TTL.get(lane.value)
        if ttl is None:
            return None

        age = entry.age(now)
        if age <= ttl:
            return None   # still fresh

        # Downgrade PUBLISH_NOW → NEXT_SLOT (Feature #3)
        if lane == Route.PUBLISH_NOW:
            logger.info(
                "⬇️  TTL: PUBLISH_NOW→NEXT_SLOT after %.0f min: %s",
                age.total_seconds() / 60, entry.title[:55],
            )
            entry.downgraded_from = Route.PUBLISH_NOW.value
            entry.route           = Route.NEXT_SLOT.value
            entry.is_breaking     = False
            self._audit(
                entry.title, entry.score, entry.category_tier,
                Route.PUBLISH_NOW, "DOWNGRADED",
                f"TTL {ttl} exceeded ({age.total_seconds()/3600:.1f}h) → NEXT_SLOT",
            )
            return None   # still publishable in new lane

        # Downgrade NEXT_SLOT → SCHEDULE (Feature #3)
        if lane == Route.NEXT_SLOT:
            logger.info(
                "⬇️  TTL: NEXT_SLOT→SCHEDULE after %.0f min: %s",
                age.total_seconds() / 60, entry.title[:55],
            )
            entry.downgraded_from = entry.downgraded_from or Route.NEXT_SLOT.value
            entry.route           = Route.SCHEDULE.value
            self._audit(
                entry.title, entry.score, entry.category_tier,
                Route.NEXT_SLOT, "DOWNGRADED",
                f"TTL {ttl} exceeded ({age.total_seconds()/3600:.1f}h) → SCHEDULE",
            )
            return None   # still publishable in new lane

        # Expire SCHEDULE and HOLD
        entry.status = "EXPIRED"
        reason = (
            f"TTL expired: {lane.value} stories live max "
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
            pf = _Path("published_titles.json")
            if pf.exists():
                compare += _json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            pass

        for other in compare:
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
        """
        Slot reservation with category mix.

        Each scheduled window publishes up to one story per tier (T1/T2/T3).
        An entry wins its slot if it is the highest-scoring SCHEDULE story
        within its own tier. This ensures every window has natural variety
        rather than publishing three politics stories back-to-back.
        """
        same_tier_queued = [
            e for e in self._entries
            if e.status == "QUEUED"
            and e.route == Route.SCHEDULE.value
            and e.category_tier == entry.category_tier
        ]
        if not same_tier_queued:
            return True
        best = max(same_tier_queued, key=lambda e: e.score)
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
                e.setdefault("category_tier",   CATEGORY_TIERS.get(e.get("category", "breaking"), 2))
                e.setdefault("route",            Route.SCHEDULE.value)
                e.setdefault("downgraded_from",  None)
                e.setdefault("image_path",       None)
                e.setdefault("post_content",     None)
                e.setdefault("card_headline",    None)
                e.setdefault("hashtags",         None)
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
