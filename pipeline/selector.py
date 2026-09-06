"""
pipeline/selector.py — Select and rank the best story candidates.

Editorial scoring follows the Global Pulse News publishing rules:

Priority order:
  1. Major breaking news
  2. Major developing stories
  3. Important national/international news
  4. Major sports/business/technology stories
  5. Significant human-interest stories
  6. Minor/trending stories

Ranking criteria:
  - News importance (source credibility + breaking keywords)
  - Urgency (freshness — newer = higher score)
  - Uniqueness (title length / specificity)
  - Summary quality (non-empty = bonus)

Stories are scored 0–100 and sorted. The highest-scoring fresh story is returned.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import config
from models import Story, StoryRejected

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# High-credibility source domains (bonus score)
# ---------------------------------------------------------------------------

TIER1_SOURCES = {
    "bbc", "reuters", "ap", "associated press", "nyt", "new york times",
    "washington post", "guardian", "bloomberg", "ft", "financial times",
    "al jazeera", "npr", "abc news", "cbs news", "nbc news", "cnn",
    "the economist", "sky news", "france 24",
}

# Keywords that indicate breaking / high-priority news
BREAKING_KEYWORDS = [
    r"\bbreaking\b", r"\burgent\b", r"\bjust in\b", r"\bdead\b", r"\bkilled\b",
    r"\battack\b", r"\bexplosion\b", r"\bearthquake\b", r"\bcrash\b", r"\bfire\b",
    r"\bcoup\b", r"\bwar\b", r"\binvasion\b", r"\bceasefire\b", r"\belection\b",
    r"\barrested\b", r"\bindicted\b", r"\bsanctions\b", r"\bemergency\b",
    r"\bdisaster\b", r"\bflood\b", r"\bhurricane\b", r"\btyphoon\b",
]

# Keywords that indicate weak / low-value content
WEAK_KEYWORDS = [
    r"\bsponsored\b", r"\baffiliate\b", r"\bpromo\b", r"\badvertis",
    r"\bclick here\b", r"\bbuy now\b", r"\bdeal of the day\b",
    r"\bhow to look\b", r"\bbest products\b", r"\btop \d+ tips\b",
]


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def select_story(stories: list[Story]) -> Story:
    """
    Score and rank all candidate stories. Return the highest-scoring fresh story.
    Raises StoryRejected if no acceptable story exists.
    """
    now             = datetime.now(timezone.utc)
    max_age_seconds = config.NEWS_MAX_AGE_HOURS * 3600

    scored: list[tuple[float, Story]] = []

    for story in stories:
        # Basic sanity checks
        if not story.title or not story.source_url:
            continue
        if "[Removed]" in story.title:
            continue

        # Age check
        pub = story.published_at
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_seconds = (now - pub).total_seconds()
        if age_seconds > max_age_seconds:
            logger.debug("Skipping old story (%dh): %s", age_seconds // 3600, story.title)
            continue

        # Reject weak/promotional content
        title_lower = story.title.lower()
        if any(re.search(p, title_lower) for p in WEAK_KEYWORDS):
            logger.debug("Skipping weak/promotional story: %s", story.title)
            continue

        score = _score(story, age_seconds)
        scored.append((score, story))

    if not scored:
        raise StoryRejected(
            f"No acceptable stories found within the last {config.NEWS_MAX_AGE_HOURS} hours."
        )

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    selected = scored[0][1]
    logger.info(
        "Selected story (score=%.1f): %s [%s]",
        scored[0][0], selected.title, selected.source_name,
    )
    return selected


def score_stories(stories: list[Story]) -> list[tuple[float, Story]]:
    """
    Return all stories scored and sorted — useful for bulk processing.
    Filters out too-old and weak stories.
    """
    now             = datetime.now(timezone.utc)
    max_age_seconds = config.NEWS_MAX_AGE_HOURS * 3600
    scored: list[tuple[float, Story]] = []

    for story in stories:
        if not story.title or not story.source_url:
            continue
        if "[Removed]" in story.title:
            continue
        pub = story.published_at
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_seconds = (now - pub).total_seconds()
        if age_seconds > max_age_seconds:
            continue
        title_lower = story.title.lower()
        if any(re.search(p, title_lower) for p in WEAK_KEYWORDS):
            continue
        scored.append((_score(story, age_seconds), story))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Scoring logic
# ---------------------------------------------------------------------------

def _score(story: Story, age_seconds: float) -> float:
    """
    Score a story 0–100 based on editorial priority rules.

    Components:
      - Freshness:       0–30 pts  (newer = higher)
      - Breaking signal: 0–25 pts  (breaking keywords in title)
      - Source tier:     0–20 pts  (credible source = higher)
      - Summary quality: 0–10 pts  (non-empty summary)
      - Title quality:   0–10 pts  (specific, not too short/long)
      - Category bonus:   0–5 pts  (breaking/world/war/crime = top priority)
    """
    score = 0.0

    # 1. Freshness — full 30 pts if <1h old, linear decay to 0 at max age
    max_age = config.NEWS_MAX_AGE_HOURS * 3600
    freshness = max(0.0, 1.0 - (age_seconds / max_age))
    score += freshness * 30

    # 2. Breaking signal
    title_lower = story.title.lower()
    breaking_hits = sum(1 for p in BREAKING_KEYWORDS if re.search(p, title_lower))
    score += min(breaking_hits * 5, 25)

    # 3. Source tier credibility
    source_lower = story.source_name.lower()
    if any(t in source_lower for t in TIER1_SOURCES):
        score += 20
    elif len(story.source_name) > 2:
        score += 8   # any named source is better than unknown

    # 4. Summary quality
    if story.raw_summary and len(story.raw_summary) > 50:
        score += 10
    elif story.raw_summary:
        score += 5

    # 5. Title quality — penalise very short or very long titles
    title_len = len(story.title)
    if 30 <= title_len <= 120:
        score += 10
    elif title_len > 120:
        score += 5

    # 6. Category priority bonus
    high_priority = {"breaking", "war", "crime", "world", "politics"}
    if getattr(story, "category", "") in high_priority:
        score += 5

    return round(score, 1)
