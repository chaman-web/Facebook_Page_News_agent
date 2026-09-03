"""
pipeline/deduplicator.py — Detect and record already-processed stories.

Uses a local JSON file (seen_stories.json) to track processed story URLs and titles.
Title similarity is checked with difflib.SequenceMatcher (stdlib, no extra deps).
"""

from __future__ import annotations

import json
import logging
from difflib import SequenceMatcher
from pathlib import Path

import config
from models import DuplicateStory, Story

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def check_duplicate(story: Story) -> None:
    """
    Check whether the story has already been processed.
    Raises DuplicateStory if a match is found.
    """
    seen = _load_seen()

    # 1. Exact URL match
    if story.source_url in seen.get("urls", []):
        raise DuplicateStory(f"Duplicate URL: {story.source_url}")

    # 2. Title similarity
    for seen_title in seen.get("titles", []):
        ratio = SequenceMatcher(None, _normalise(story.title), _normalise(seen_title)).ratio()
        if ratio >= config.DUPLICATE_TITLE_THRESHOLD:
            raise DuplicateStory(
                f"Similar title ({ratio:.0%} match): '{story.title}' ≈ '{seen_title}'"
            )

    logger.debug("Story is not a duplicate: %s", story.title)


def mark_seen(story: Story) -> None:
    """Append the story to seen_stories.json so future runs can detect it as a duplicate."""
    seen = _load_seen()
    seen.setdefault("urls", []).append(story.source_url)
    seen.setdefault("titles", []).append(story.title)
    _save_seen(seen)
    logger.debug("Marked as seen: %s", story.title)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_seen() -> dict:
    path = Path(config.SEEN_STORIES_PATH)
    if not path.exists():
        return {"urls": [], "titles": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Support both legacy list format and new dict format
        if isinstance(data, list):
            return {"urls": [], "titles": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"urls": [], "titles": []}


def _save_seen(seen: dict) -> None:
    path = Path(config.SEEN_STORIES_PATH)
    path.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalise(title: str) -> str:
    """Lowercase and strip punctuation for more reliable fuzzy comparison."""
    return "".join(c.lower() for c in title if c.isalnum() or c.isspace()).strip()
