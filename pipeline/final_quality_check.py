"""
pipeline/final_quality_check.py — Final editorial gate before Facebook publishing.

Hard checks (block publish — genuinely unfixable):
  1. Story too old (>48h)
  2. Post content empty
  3. Facebook policy violations
  4. Near-duplicate of recently published post

Soft checks (warn only; the publisher restores a branded image fallback):
  - Page hashtag absent
  - Sources line absent
  - No closing question

Philosophy: fix it, don't reject it. This gate stops harmful/stale content only.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import config
from models import Story

logger = logging.getLogger(__name__)

_RECENT_POSTS: list[str] = []
_MAX_RECENT = 20
_MIN_UNIQUE = 0.4  # posts must be ≥40% different from any recent post

_FORBIDDEN_PATTERNS = [
    r"\bbuy now\b",
    r"\bclick here\b",
    r"\bfree money\b",
    r"\bmake money fast\b",
    r"\bget rich\b",
    r"\b100%\s+guaranteed\b",
    r"\blose weight fast\b",
    r"\bmiracle cure\b",
]


class FinalQualityError(Exception):
    """Raised when a post fails a hard final quality check."""


def final_quality_check(story: Story, image_path: Path | None = None) -> None:
    """
    Run final quality checks before publishing.
    Hard failures raise FinalQualityError.
    Soft issues log warnings and never block publishing.
    """
    post  = story.post_content or ""
    lower = post.lower()

    # ── HARD: story freshness ────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    pub = story.published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    age_h = (now - pub).total_seconds() / 3600
    if age_h > config.NEWS_MAX_AGE_HOURS:
        raise FinalQualityError(
            f"Story is {age_h:.1f}h old — exceeds max age of {config.NEWS_MAX_AGE_HOURS}h."
        )

    # ── HARD: post not empty ─────────────────────────────────────────────────
    if not post or len(post.strip()) < 60:
        raise FinalQualityError("Post content is empty or too short to publish.")

    # ── HARD: Facebook policy violations ────────────────────────────────────
    for pattern in _FORBIDDEN_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            raise FinalQualityError(f"Post contains policy-violating phrase: '{pattern}'")

    # ── HARD: near-duplicate of recently published post ──────────────────────
    if _RECENT_POSTS and post:
        from difflib import SequenceMatcher
        for recent in _RECENT_POSTS[-_MAX_RECENT:]:
            ratio = SequenceMatcher(None, post[:500], recent[:500]).ratio()
            if ratio > (1 - _MIN_UNIQUE):
                raise FinalQualityError(
                    f"Post is too similar to a recently published post ({ratio:.0%} match)."
                )

    # ── SOFT warnings — never block ──────────────────────────────────────────
    if image_path is not None and not Path(image_path).exists():
        logger.warning("Final check: image not found on disk — publisher will create a branded fallback.")

    if not any(t in lower for t in ("#globalpulsenews", "#worldupdate")):
        logger.warning("Final check: page hashtag missing.")

    if "sources:" not in lower:
        logger.warning("Final check: sources line missing.")

    if "?" not in post and "👇" not in post:
        logger.warning("Final check: no closing question.")

    # ── All hard checks passed ───────────────────────────────────────────────
    logger.info("✅ Final quality check passed.")
    _RECENT_POSTS.append(post)
    if len(_RECENT_POSTS) > _MAX_RECENT:
        _RECENT_POSTS.pop(0)
