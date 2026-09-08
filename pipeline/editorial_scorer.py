"""
pipeline/editorial_scorer.py — Editorial Scoring Engine for Global Pulse News.

Every verified story is scored before any post is generated.
Only stories that meet the minimum threshold are published.

SCORING BREAKDOWN (100 points total):
┌──────────────────────────────────────────┬────────┐
│ Criterion                                │  Max   │
├──────────────────────────────────────────┼────────┤
│ 1. News Value                            │   30   │
│ 2. Breaking / Urgency (+corroboration)   │   20   │
│ 3. Audience Interest (+geo +title)       │   20   │
│ 4. Source Credibility                    │   15   │
│ 5. Freshness (−stale-event penalty)      │   10   │
│ 6. Visual Potential                      │    5   │
└──────────────────────────────────────────┴────────┘
│ TOTAL (pre-multiplier)                   │  100   │
│ Category multiplier applied after total  │        │
│   breaking/war/crime/world → ×1.10       │        │
│   trending/entertainment   → ×0.95       │        │
└──────────────────────────────────────────┴────────┘

THRESHOLDS (after multiplier, capped at 100):
  90–100 → PRIORITY / BREAKING   → publish immediately, bypass interval
  80–89  → HIGH PRIORITY         → publish in next slot
  70–79  → PUBLISH               → normal queue
  60–69  → SCHEDULE / HOLD       → filler only — publish if nothing better available
  < 60   → DO NOT PUBLISH        → rejected outright

VERIFICATION IS A SEPARATE GATE:
  This module measures editorial importance only. A high score preserves a
  powerful single-source story for review, but does not make it eligible for
  automatic publishing. pipeline.verifier owns that decision.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import config
from models import Story

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & result dataclass
# ---------------------------------------------------------------------------

class EditorialTier(str, Enum):
    PRIORITY = "PRIORITY / BREAKING"   # 90–100
    HIGH     = "HIGH PRIORITY"         # 80–89
    PUBLISH  = "PUBLISH"               # 70–79
    HOLD     = "SCHEDULE / HOLD"       # 60–69
    REJECT   = "DO NOT PUBLISH"        # < 60


@dataclass
class EditorialScore:
    story_title:          str
    news_value:           float   # /30
    breaking_urgency:     float   # /20
    audience_interest:    float   # /20
    source_credibility:   float   # /15
    freshness:            float   # /10
    visual_potential:     float   # /5
    category_multiplier:  float
    total:                float   # /100 (after multiplier)
    tier:                 EditorialTier
    reason:               str
    effective_tier_num:   int     = 2   # actual tier used (may differ from category default)
    tier_was_overridden:  bool    = False

    def is_publishable(self) -> bool:
        return self.tier != EditorialTier.REJECT

    def is_breaking(self) -> bool:
        return self.tier == EditorialTier.PRIORITY

    def is_filler(self) -> bool:
        """HOLD stories are fillers — only publish when nothing better available."""
        return self.tier == EditorialTier.HOLD

    def skip_queue_interval(self) -> bool:
        """PRIORITY and HIGH bypass the minimum posting interval."""
        return self.tier in (EditorialTier.PRIORITY, EditorialTier.HIGH)


# ---------------------------------------------------------------------------
# Keyword tables
# ---------------------------------------------------------------------------

# Breaking / urgency signals — (pattern, max_points)
# We take the single highest match rather than adding all of them
_BREAKING_SIGNALS = [
    (r"\bnuclear\b",              20),
    (r"\bcoup\b",                 19),
    (r"\binvasion\b",             19),
    (r"\bterror(ist|ism)?\b",     18),
    (r"\bassassin(ation|ated)?\b",18),
    (r"\bbreaking\b",             18),
    (r"\burgent\b",               17),
    (r"\bjust in\b",              17),
    (r"\bmissile(s)?\b",          16),
    (r"\bwar\b",                  15),
    (r"\bceasefire\b",            15),
    (r"\bkilled\b",               15),
    (r"\bdead\b",                 14),
    (r"\battack(ed|s)?\b",        14),
    (r"\bexplosion\b",            14),
    (r"\bearthquake\b",           14),
    (r"\bemergency\b",            14),
    (r"\bdisaster\b",             13),
    (r"\bhurricane\b",            13),
    (r"\btyphoon\b",              13),
    (r"\btsunami\b",              14),
    (r"\bshooting(s)?\b",         13),
    (r"\barrested\b",             12),
    (r"\bindicted\b",             12),
    (r"\bsanctions\b",            10),
    (r"\belection(s)?\b",         10),
    (r"\bcrash(ed)?\b",           12),
    (r"\bflood(s|ing)?\b",        12),
    (r"\brescue\b",               10),
    (r"\bfire\b",                 10),
    (r"\bdrone(s)?\b",            10),
]

# High news-value topics (global importance)
_HIGH_NEWS_VALUE = [
    r"\bwar\b", r"\binvasion\b", r"\bconflict\b", r"\bbattle\b",
    r"\bnuclear\b", r"\bmissile\b", r"\bsanctions\b",
    r"\belection\b", r"\bcoup\b", r"\bgovernment\b",
    r"\bpresident\b", r"\bprime minister\b", r"\bparliament\b",
    r"\bclimate\b", r"\bglobal warming\b", r"\bdisaster\b",
    r"\bpandemic\b", r"\bvirus\b", r"\boutbreak\b", r"\bvaccine\b",
    r"\beconomy\b", r"\brecession\b", r"\bcrisis\b", r"\binfla(tion)?\b",
    r"\bterror(ism|ist)?\b", r"\battack\b", r"\bbomb(ing)?\b",
    r"\bhumanitarian\b", r"\brefugee\b",
    r"\bhuman rights\b", r"\bgenocide\b", r"\bwar crime\b",
    r"\bcorruption\b", r"\bscandal\b", r"\bindicted\b", r"\btrial\b",
]

# Low-value / soft content — hard penalty
_LOW_VALUE_PATTERNS = [
    r"\bbest \d+\b", r"\btop \d+\b",
    r"\bhow to look\b", r"\bproduct review\b",
    r"\bsponsored\b", r"\baffiliate\b", r"\bdeal of the day\b",
    r"\bbuy now\b", r"\bclick here\b",
    r"\bopinion:\b", r"\bcommentary:\b", r"\bcolumn:\b",
    r"\bpodcast\b", r"\bnewsletter\b",
    r"\bwatch:\b", r"\bpreview:\b", r"\breview:\b",
    r"\bguide to\b", r"\bwhat to watch\b",
    r"\bbest movies\b", r"\bbest shows\b", r"\bstreaming now\b",
    r"\bwhy .{0,30} matters\b", r"\bwhat we know\b",
    r"\banalysis:\b",
]

# Opinion/commentary — reduces news value
_OPINION_PATTERNS = [
    r"^opinion[:\s]",
    r"^analysis[:\s]",
    r"^commentary[:\s]",
    r"^explainer[:\s]",
    r"\bwhy .{0,40} matters\b",
    r"\bwhat it means\b",
    r"\bhere'?s? what (we know|you need to know)\b",
]

# Audience interest topics with high Facebook engagement
_INTEREST_TOPICS = [
    r"\bwar\b", r"\bconflict\b", r"\belection\b", r"\bpresident\b",
    r"\bviral\b", r"\brecord\b", r"\bhistoric\b", r"\bfirst (ever|time)\b",
    r"\bsurprise\b", r"\bshocking\b", r"\bunbelievable\b",
    r"\bfootball\b", r"\bsoccer\b", r"\bcricket\b", r"\bolympic\b",
    r"\bclimate\b", r"\bartificial intelligence\b", r"\b\bai\b\b",
    r"\bjobs\b", r"\bunemployment\b", r"\beconomy\b",
    r"\bcancer\b", r"\bcure\b", r"\bbreakthrough\b", r"\bdiscovery\b",
    r"\bchildren\b", r"\bschool\b",
    r"\bcorruption\b", r"\bscandal\b",
]

# Geographic reach — global/multi-country stories score higher
_GLOBAL_REACH = [
    r"\bglobal\b", r"\bworldwide\b", r"\binternational\b",
    r"\bun\b", r"\bunited nations\b", r"\bnato\b", r"\bg7\b", r"\bg20\b",
    r"\bmultiple countr\b", r"\bacross the world\b", r"\baround the world\b",
    r"\beu\b", r"\beuropean union\b", r"\bimf\b", r"\bworld bank\b",
]

# Title structure signals — proven engagement drivers on Facebook
_ENGAGEMENT_TITLE_SIGNALS = [
    r"\d+\s+(dead|killed|injured|wounded|missing|arrested)",   # numbers + outcome
    r"\d+\s*(people|civilians|soldiers|workers|children)",     # numbers + group
    r"\bfirst (ever|time|in history)\b",                       # historic first
    r"\brecord\b",                                             # record-breaking
    r"\b(who|what|why|how)\b.{0,40}\?$",                       # question title
    r"\b(says|warns|vows|urges|demands|reveals|confirms)\b",   # strong verb
]

# Stale event signals — article is new but the event is old
_STALE_EVENT_SIGNALS = [
    r"\blast (week|month|year)\b",
    r"\bdays ago\b",
    r"\bearlier this (week|month|year)\b",
    r"\b(last|previous) (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\bin \d{4}\b",  # references to past years
    r"\bdecade(s)? ago\b",
]

# Visual potential — stories with compelling imagery
_VISUAL_TOPICS = [
    r"\bfire\b", r"\bexplosion\b", r"\bflood\b", r"\bearthquake\b",
    r"\bhurricane\b", r"\bstorm\b", r"\bprotests?\b",
    r"\bspace\b", r"\brocket\b", r"\blaunch\b", r"\bsatellite\b",
    r"\bsport\b", r"\bfootball\b", r"\bcricket\b", r"\bolympic\b",
    r"\bfestival\b", r"\bcelebration\b", r"\bparade\b",
    r"\brescue\b", r"\bsurvivor\b", r"\bmilitary\b",
    r"\bpresident\b", r"\bprime minister\b", r"\bsummit\b",
    r"\bwar\b", r"\btroops\b", r"\bbattle\b",
]

# ---------------------------------------------------------------------------
# Category tier system
# ---------------------------------------------------------------------------
#
# Tier 1 — Immediate priority (major global impact)
#   breaking, war, world, crime, politics
#   Multiplier: ×1.15  |  Score floor boost: +8 pts before multiplier
#
# Tier 2 — Strong regular content
#   sports, business, technology, climate, science
#   Multiplier: ×1.05  |  No floor boost
#
# Tier 3 — Supporting content
#   entertainment, trending, jobs, wellness
#   Multiplier: ×0.92  |  Score cap: 85 (can never outrank a major Tier 1 story)
#
# A Tier 3 story can still score 70–84 and get published — it just cannot
# beat a Tier 1 story for the same publishing slot.
# ---------------------------------------------------------------------------

CATEGORY_TIERS = {
    # Tier 1 — Immediate priority
    "breaking":      1,
    "war":           1,
    "world":         1,
    "crime":         1,
    "politics":      1,
    # Tier 2 — Strong regular
    "sports":        2,
    "business":      2,
    "technology":    2,
    "climate":       2,
    "science":       2,
    # Tier 3 — Supporting
    "entertainment": 3,
    "trending":      3,
    "jobs":          3,
    "wellness":      3,
}

_CATEGORY_MULTIPLIERS = {
    # Tier 1
    "breaking":      1.15,
    "war":           1.15,
    "world":         1.12,
    "crime":         1.10,
    "politics":      1.10,
    # Tier 2
    "sports":        1.05,
    "business":      1.05,
    "technology":    1.05,
    "climate":       1.05,
    "science":       1.05,
    # Tier 3
    "entertainment": 0.92,
    "trending":      0.92,
    "jobs":          0.95,
    "wellness":      0.95,
}

# Floor score boost added BEFORE the multiplier for Tier 1 stories
_TIER1_FLOOR_BOOST = 8.0

# Score cap for Tier 3 stories — prevents soft content from outranking hard news
_TIER3_SCORE_CAP = 85.0

# ---------------------------------------------------------------------------
# Feature #2 — Story-level tier override
# If a Tier 2 or Tier 3 story contains these signals it gets promoted to Tier 1
# and receives the Tier 1 multiplier + floor boost regardless of its category.
# Example: celebrity death → Entertainment (T3) but death signal → promoted to T1
# ---------------------------------------------------------------------------

_TIER1_OVERRIDE_SIGNALS = [
    # Death / mass casualty events
    (r"\b(dead|killed|deaths?|casualties)\b", r"\b(\d{2,}|many|mass(ive)?|dozens?|hundreds?)\b"),
    # Famous person death
    (r"\b(dies|died|death|passed away|passed on)\b", r"\b(president|minister|celebrity|star|famous|icon|legend)\b"),
    # Major scandal / legal action on a public figure
    (r"\b(arrested|indicted|charged|convicted|sentenced)\b", r"\b(president|minister|celebrity|executive|ceo|senator|governor)\b"),
    # Financial catastrophe
    (r"\b(bankrupt|collapse(d|s)?|crash(ed)?)\b", r"\b(billion|economy|market|bank|currency|stock)\b"),
    # Mass shooting / terror attack
    (r"\b(shooting|attack|bomb(ing)?|explosion)\b", r"\b(school|hospital|church|mosque|concert|market|airport)\b"),
    # Natural disaster at scale
    (r"\b(earthquake|tsunami|hurricane|typhoon|flood)\b", r"\b(kills?|dead|missing|devastat|destroy)\b"),
    # Historic / unprecedented event
    (r"\b(historic|unprecedented|first time in history|record-breaking)\b", r"\b(war|peace|crisis|disaster|election)\b"),
]

def _detect_tier_override(text: str, category: str) -> int | None:
    """
    Check if a story from Tier 2 or Tier 3 should be promoted to Tier 1.
    Returns 1 if promoted, None if no override.
    Only applies to Tier 2/3 — Tier 1 stories are already at the top.
    """
    current_tier = CATEGORY_TIERS.get(category, 2)
    if current_tier == 1:
        return None   # Already Tier 1

    t = text.lower()
    for pattern_a, pattern_b in _TIER1_OVERRIDE_SIGNALS:
        if re.search(pattern_a, t, re.IGNORECASE) and re.search(pattern_b, t, re.IGNORECASE):
            logger.debug(
                "Story-level Tier1 override triggered for category '%s': %s + %s",
                category, pattern_a, pattern_b,
            )
            return 1
    return None

def score_story(story: Story) -> EditorialScore:
    """
    Score a single story across all 6 criteria + category multiplier.
    Returns a full EditorialScore with tier and breakdown.
    """
    title   = story.title or ""
    summary = story.raw_summary or ""
    text    = (title + " " + summary).lower()
    category = getattr(story, "category", "breaking")

    news_value         = _score_news_value(text, title)
    breaking_urgency   = _score_breaking_urgency(text, story.corroborating_sources)
    audience_interest  = _score_audience_interest(text, title)
    source_credibility = _score_source_credibility(story)
    freshness          = _score_freshness(story.published_at, text)
    visual_potential   = _score_visual_potential(text)

    raw_total = (
        news_value + breaking_urgency + audience_interest
        + source_credibility + freshness + visual_potential
    )

    tier_num   = CATEGORY_TIERS.get(category, 2)
    multiplier = _CATEGORY_MULTIPLIERS.get(category, 1.0)

    # --- Feature #2: story-level tier override ---
    # A Tier 2/3 story with breaking signals gets promoted to Tier 1
    override = _detect_tier_override(text, category)
    if override == 1:
        original_tier = tier_num
        tier_num   = 1
        multiplier = 1.12   # promotion multiplier (between war ×1.15 and world ×1.12)
        logger.info(
            "⬆ Tier promoted T%d→T1 (story-level override): %s",
            original_tier, title[:65],
        )

    # Tier 1: add floor boost before multiplier
    if tier_num == 1:
        raw_total = min(raw_total + _TIER1_FLOOR_BOOST, 100.0)

    total = round(raw_total * multiplier, 1)

    # Tier 3: cap so soft content can never outrank hard news
    if tier_num == 3:
        total = min(total, _TIER3_SCORE_CAP)

    total = min(total, 100.0)

    tier  = _classify_tier(total)

    reason = _build_reason(
        news_value, breaking_urgency, audience_interest,
        source_credibility, freshness, visual_potential,
        multiplier, tier_num, total, tier, overridden=(override == 1),
    )
    verification = getattr(story, "verification_status", "UNVERIFIED")
    verification_value = getattr(verification, "value", str(verification))
    verification_score = float(getattr(story, "verification_score", 0.0) or 0.0)
    reason += f" | Verification {verification_value} {verification_score:.0f}/100 (separate gate)"

    score = EditorialScore(
        story_title          = title,
        news_value           = news_value,
        breaking_urgency     = breaking_urgency,
        audience_interest    = audience_interest,
        source_credibility   = source_credibility,
        freshness            = freshness,
        visual_potential     = visual_potential,
        category_multiplier  = multiplier,
        total                = total,
        tier                 = tier,
        reason               = reason,
        effective_tier_num   = tier_num,
        tier_was_overridden  = (override == 1),
    )

    _log_score(score)
    return score


def score_and_filter(
    stories: list[Story],
    allow_hold: bool = False,
    preserve_verified_hold: bool = False,
) -> list[tuple[EditorialScore, Story]]:
    """
    Score all stories. Filter out REJECT tier.
    If allow_hold=False (default), also filter out HOLD tier unless no better story exists.
    Sort by total score descending.

    allow_hold=True  → include HOLD stories (used when nothing better is available)
    allow_hold=False → only PUBLISH / HIGH / PRIORITY stories
    """
    all_scored: list[tuple[EditorialScore, Story]] = []
    rejected = 0

    for story in stories:
        s = score_story(story)
        if s.is_publishable():
            all_scored.append((s, story))
        else:
            rejected += 1

    all_scored.sort(key=lambda x: x[0].total, reverse=True)

    # Separate tiers
    strong  = [(s, st) for s, st in all_scored if not s.is_filler()]
    fillers = [(s, st) for s, st in all_scored if s.is_filler()]

    if strong:
        results = strong
        if preserve_verified_hold:
            from models import VerificationStatus
            results += [
                (score, story) for score, story in fillers
                if story.verification_status == VerificationStatus.VERIFIED
            ]
    elif allow_hold and fillers:
        logger.info("No strong stories available — using %d HOLD filler(s).", len(fillers))
        results = fillers
    elif fillers:
        logger.info(
            "%d HOLD stories available but no strong stories. "
            "Use allow_hold=True to include them.", len(fillers)
        )
        results = []
    else:
        results = []

    logger.info(
        "Editorial gate: %d strong, %d hold, %d rejected (score < 60) from %d stories.",
        len(strong), len(fillers), rejected, len(stories),
    )

    if results:
        top = results[0]
        logger.info(
            "▶ Top story: [%.1f — %s] %s",
            top[0].total, top[0].tier.value, top[1].title[:70],
        )

    return results


# ---------------------------------------------------------------------------
# Criterion scorers
# ---------------------------------------------------------------------------

def _score_news_value(text: str, title: str) -> float:
    """News value (0–30)."""

    # Hard penalty for low-value/promotional/soft content
    for pattern in _LOW_VALUE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return 2.0

    # Soft penalty for opinion/commentary/analysis
    for pattern in _OPINION_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            return max(2.0, 8.0)   # opinion gets capped at 8 news value

    # Count high-importance topic hits
    hits  = sum(1 for p in _HIGH_NEWS_VALUE if re.search(p, text, re.IGNORECASE))
    score = min(hits * 5, 25)

    # Baseline for any real news story — raised so genuine stories are not buried
    if score == 0:
        score = 15.0

    # Summary quality bonus
    if len(text) > 200:
        score += 5

    return min(score, 30)


def _score_breaking_urgency(text: str, corroborating: list[dict] | None) -> float:
    """
    Breaking / urgency (0–20).
    Takes the single highest signal match (not additive — prevents keyword stuffing).
    Adds a corroboration urgency bonus: confirmed breaking = more urgent.
    """
    best = 0
    for pattern, pts in _BREAKING_SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            best = max(best, pts)

    # Corroboration urgency bonus — if story is confirmed AND breaking, it's more urgent
    corr_count = len(corroborating or [])
    if best >= 12 and corr_count >= 2:
        best = min(best + 2, 20)   # confirmed breaking gets +2
    elif best >= 12 and corr_count >= 1:
        best = min(best + 1, 20)

    # Baseline — any story gets a floor score
    if best == 0:
        best = 8

    return min(float(best), 20.0)


def _score_audience_interest(text: str, title: str) -> float:
    """
    Audience interest (0–20).
    Includes geographic reach signal and engagement title structure.
    """
    # Base interest topics
    hits  = sum(1 for p in _INTEREST_TOPICS if re.search(p, text, re.IGNORECASE))
    score = min(hits * 3, 14)

    # Baseline — real stories always have some audience interest
    if score == 0:
        score = 8.0

    # Geographic reach bonus — global stories reach more people
    global_hits = sum(1 for p in _GLOBAL_REACH if re.search(p, text, re.IGNORECASE))
    score += min(global_hits * 1.5, 3.0)

    # Title structure bonus — proven engagement drivers
    for pattern in _ENGAGEMENT_TITLE_SIGNALS:
        if re.search(pattern, title, re.IGNORECASE):
            score += 1.5
            break   # Only one bonus per story

    return min(score, 20.0)


def _score_source_credibility(story: Story) -> float:
    """
    Source credibility (0–15).
    Uses the central source classifier tier instead of a second name list.
    Tier-1 = 10, Tier-2 = 7, Tier-3 = 5, Tier-4 = 2.
    +1 per independent reliable corroborating domain, capped at +5.
    """
    from pipeline.source_classifier import SourceTier, canonical_domain, classify_source

    source_tier = int(getattr(story, "source_tier", SourceTier.TIER4))
    score = {
        int(SourceTier.TIER1): 10.0,
        int(SourceTier.TIER2): 7.0,
        int(SourceTier.TIER3): 5.0,
        int(SourceTier.TIER4): 2.0,
    }.get(source_tier, 0.0)

    seen = {canonical_domain(story.source_url)}
    independent = 0
    for source in story.corroborating_sources or []:
        tier = source.get("tier")
        if tier is None:
            tier = classify_source(source.get("name", ""), source.get("url", "")).tier
        domain = canonical_domain(source.get("url") or source.get("domain", ""))
        if int(tier) <= int(SourceTier.TIER3) and domain and domain not in seen:
            seen.add(domain)
            independent += 1

    score += min(independent, 5)
    return min(score, 15.0)


def _score_freshness(published_at: datetime, text: str) -> float:
    """
    Freshness (0–10).
    Age-based scoring with stale-event penalty:
    If the article is new but the EVENT is old (e.g. "last week"), penalise.
    """
    now = datetime.now(timezone.utc)
    pub = published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    age_h = (now - pub).total_seconds() / 3600

    if   age_h <  1:  score = 10.0
    elif age_h <  3:  score = 8.0
    elif age_h <  6:  score = 6.0
    elif age_h < 12:  score = 5.0
    elif age_h < 24:  score = 3.0
    elif age_h < 48:  score = 1.0
    else:             score = 0.0

    # Stale-event penalty — article is new but the story happened earlier
    stale_hits = sum(1 for p in _STALE_EVENT_SIGNALS if re.search(p, text, re.IGNORECASE))
    if stale_hits >= 2:
        score = max(0.0, score - 4.0)   # heavy penalty
    elif stale_hits == 1:
        score = max(0.0, score - 2.0)   # light penalty

    return score


def _score_visual_potential(text: str) -> float:
    """Visual potential (0–5)."""
    hits  = sum(1 for p in _VISUAL_TOPICS if re.search(p, text, re.IGNORECASE))
    score = min(hits * 1.0, 5.0)
    return max(score, 1.0)   # baseline of 1 for any story


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

def _classify_tier(total: float) -> EditorialTier:
    if total >= 90: return EditorialTier.PRIORITY
    if total >= 80: return EditorialTier.HIGH
    if total >= 70: return EditorialTier.PUBLISH
    if total >= 60: return EditorialTier.HOLD
    return EditorialTier.REJECT


# ---------------------------------------------------------------------------
# Formatting & logging
# ---------------------------------------------------------------------------

def _build_reason(
    nv: float, bu: float, ai: float, sc: float,
    fr: float, vp: float, mult: float, tier_num: int,
    total: float, tier: EditorialTier, overridden: bool = False,
) -> str:
    tier_labels = {1: "Tier1-PRIORITY", 2: "Tier2-STRONG", 3: "Tier3-SUPPORT"}
    mult_str    = f"×{mult:.2f}"
    cap_note    = " [capped@85]" if tier_num == 3 and total >= 85 else ""
    boost_note  = " [+8 floor]"  if tier_num == 1 else ""
    override_note = " [⬆PROMOTED]" if overridden else ""
    return (
        f"NewsVal {nv:.0f}/30 | Breaking {bu:.0f}/20 | "
        f"Audience {ai:.0f}/20 | Credibility {sc:.0f}/15 | "
        f"Freshness {fr:.0f}/10 | Visual {vp:.0f}/5 | "
        f"Cat {mult_str}{boost_note} ({tier_labels[tier_num]}){override_note} → "
        f"{total:.1f}/100{cap_note} [{tier.value}]"
    )


def _log_score(s: EditorialScore) -> None:
    icon = {
        EditorialTier.PRIORITY: "🔴",
        EditorialTier.HIGH:     "🟠",
        EditorialTier.PUBLISH:  "🟢",
        EditorialTier.HOLD:     "🟡",
        EditorialTier.REJECT:   "⚫",
    }[s.tier]
    logger.info("%s [%5.1f] %-22s %s", icon, s.total, s.tier.value, s.story_title[:65])
    logger.debug("         %s", s.reason)
