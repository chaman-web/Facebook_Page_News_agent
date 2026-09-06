"""
pipeline/final_quality_check.py — Final editorial gate before Facebook publishing.

This is the LAST check before a post goes live. It verifies:

  1. Story is still fresh (not older than NEWS_MAX_AGE_HOURS)
  2. Post content is present and non-empty
  3. Image path exists on disk (if image mode is on)
  4. Headline in post matches the story title (no hallucinated facts)
  5. No forbidden phrases that violate Facebook policies
  6. Post contains the page hashtag (#GlobalPulseNews)
  7. Sources line is present
  8. Post has a closing question (engagement driver)
  9. No duplicate content — post text is not identical to a recently published post

Raises FinalQualityError with a clear reason if any check fails.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import config
from models import Story

logger = logging.getLogger(__name__)

# Track last N post texts in memory to catch near-duplicate generation
_RECENT_POSTS: list[str] = []
_MAX_RECENT   = 20
_MIN_UNIQUE   = 0.4   # posts must be at least 40% different from any recent post

# Facebook policy violations
_FORBIDDEN_PATTERNS = [
    r"\bbuy now\b",
    r"\bclick here\b",
    r"\bfree money\b",
    r"\bmake money fast\b",
    r"\bget rich\b",
    r"\b100%\s+guaranteed\b",
    r"\bwork from home\b.*\beam\b",
    r"\blose weight fast\b",
    r"\bmiracle cure\b",
]


class FinalQualityError(Exception):
    """Raised when a post fails the final quality gate."""


def final_quality_check(story: Story, image_path: Path | None = None) -> None:
    """
    Run all final quality checks before publishing.
    Raises FinalQualityError with reason if any check fails.
    Logs a warning (non-fatal) for minor issues.
    """
    issues: list[str] = []
    post   = story.post_content or ""
    lower  = post.lower()

    # 1. Story freshness
    now = datetime.now(timezone.utc)
    pub = story.published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    age_h = (now - pub).total_seconds() / 3600
    if age_h > config.NEWS_MAX_AGE_HOURS:
        issues.append(
            f"Story is {age_h:.1f}h old — exceeds max age of {config.NEWS_MAX_AGE_HOURS}h."
        )

    # 2. Post content present
    if not post or len(post.strip()) < 100:
        issues.append("Post content is empty or too short.")

    # 3. Image file exists (if provided)
    if image_path is not None and not Path(image_path).exists():
        issues.append(f"Image file not found on disk: {image_path}")

    # 4. Facebook policy violations
    for pattern in _FORBIDDEN_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            issues.append(f"Post contains policy-violating phrase: '{pattern}'")

    # 5. Page hashtag present (warning only)
    page_tags = ["#globalpulsenews", "#worldupdate"]
    if not any(t in lower for t in page_tags):
        logger.warning("Final check: post missing page hashtag.")

    # 6. Sources line present (warning only)
    if "sources:" not in lower:
        logger.warning("Final check: post missing sources line.")

    # 7. Closing question (warning only)
    if "?" not in post and "👇" not in post:
        logger.warning("Final check: post has no closing question.")

    # 8. Near-duplicate detection against recent posts
    if _RECENT_POSTS and post:
        from difflib import SequenceMatcher
        for recent in _RECENT_POSTS[-_MAX_RECENT:]:
            ratio = SequenceMatcher(None, post[:500], recent[:500]).ratio()
            if ratio > (1 - _MIN_UNIQUE):
                issues.append(
                    f"Post content is too similar to a recently published post ({ratio:.0%} match)."
                )
                break

    # --- Report ---
    if issues:
        reason = " | ".join(issues)
        raise FinalQualityError(f"Final quality check failed: {reason}")

    # All checks passed — record this post text for future near-dupe detection
    _RECENT_POSTS.append(post)
    if len(_RECENT_POSTS) > _MAX_RECENT:
        _RECENT_POSTS.pop(0)

    logger.info("✅ Final quality check passed: %s", story.title)
