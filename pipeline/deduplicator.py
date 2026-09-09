"""
pipeline/deduplicator.py — Detect and record already-processed stories.

Uses a local JSON file (seen_stories.json) to track processed story URLs and titles.
Title similarity is checked with difflib.SequenceMatcher (stdlib, no extra deps).

Attempt tracking:
  - Stories are marked seen immediately after passing dedup (Stage 2).
  - Unverified/held stories are blocked after MAX_ATTEMPTS runs.
  - Published stories are marked permanent=True and blocked forever.

TTL pruning:
  - Attempt-tracked entries older than TTL_DAYS are pruned on load.
  - Permanent entries (published stories) are never pruned.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import config
from models import DuplicateStory, Story

logger = logging.getLogger(__name__)

# Max times an unverified/held story is allowed to re-enter the pipeline
MAX_ATTEMPTS = 3

# Attempt-tracked entries older than this are pruned (published entries kept forever)
TTL_DAYS = 7

_DEVELOPMENT_SIGNALS = re.compile(
    r"\b(?:confirmed|announced|approved|rejected|resigned|arrested|charged|"
    r"convicted|sentenced|killed|died|won|lost|launched|recovered|rescued|"
    r"ceasefire|deal reached|rises? to|falls? to|death toll|results?)\b",
    re.IGNORECASE,
)


def is_meaningful_update(new_title: str, previous_title: str) -> bool:
    """Identify a concrete new development in an otherwise similar event."""
    new_norm = _normalise(new_title)
    old_norm = _normalise(previous_title)
    new_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", new_norm))
    old_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", old_norm))
    if new_numbers and new_numbers != old_numbers:
        return True
    return bool(_DEVELOPMENT_SIGNALS.search(new_title) and not _DEVELOPMENT_SIGNALS.search(previous_title))


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def check_duplicate(story: Story) -> None:
    """
    Check whether the story has already been processed.
    Raises DuplicateStory if a match is found or attempt limit exceeded.
    """
    seen = _load_seen()  # already pruned on load
    _check_duplicate_in_state(story, seen, _build_title_index(seen))


def filter_fresh_stories(
    stories: list[Story], record_attempts: bool = True
) -> tuple[list[Story], int]:
    """Check and record a pipeline batch with one file read and one file write."""
    seen = _load_seen()
    fresh: list[Story] = []
    duplicate_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    title_index = _build_title_index(seen)

    for story in stories:
        try:
            _check_duplicate_in_state(story, seen, title_index)
        except DuplicateStory:
            duplicate_count += 1
            continue
        fresh.append(story)
        if record_attempts:
            _record_seen(seen, story, permanent=False, now_iso=now_iso)

    if record_attempts and fresh:
        _save_seen(seen)
    return fresh, duplicate_count


def _check_duplicate_in_state(story: Story, seen: dict, title_index: dict | None = None) -> None:
    """Check one story against an already-loaded state mapping."""

    norm = _normalise(story.title)

    # 1. Permanent block — exact URL
    if story.source_url in seen.get("urls", []):
        raise DuplicateStory(f"Duplicate URL: {story.source_url}")

    # 2. Permanent block — similar title
    title_index = title_index or _build_title_index(seen)
    for seen_title, seen_norm in _candidate_titles(norm, title_index, "permanent"):
        ratio = SequenceMatcher(None, norm, seen_norm).ratio()
        if ratio >= config.DUPLICATE_TITLE_THRESHOLD:
            if is_meaningful_update(story.title, seen_title):
                logger.info("Meaningful update allowed through duplicate history: %s", story.title[:70])
                continue
            raise DuplicateStory(
                f"Similar title ({ratio:.0%} match): '{story.title}' ≈ '{seen_title}'"
            )

    protected = bool(getattr(story, "priority_protected", False))

    # 3. Attempt-tracked URL — block after MAX_ATTEMPTS
    url_attempts = seen.get("url_attempts", {})
    if not protected and story.source_url in url_attempts:
        count = url_attempts[story.source_url]["count"]
        if count >= MAX_ATTEMPTS:
            raise DuplicateStory(
                f"Attempt limit ({MAX_ATTEMPTS}) reached for URL: {story.source_url}"
            )

    # 4. Attempt-tracked title similarity — block after MAX_ATTEMPTS
    title_attempts = seen.get("title_attempts", {})
    for seen_title, seen_norm in ([] if protected else _candidate_titles(norm, title_index, "attempts")):
        entry = title_attempts[seen_title]
        ratio = SequenceMatcher(None, norm, seen_norm).ratio()
        if ratio >= config.DUPLICATE_TITLE_THRESHOLD:
            if entry["count"] >= MAX_ATTEMPTS:
                raise DuplicateStory(
                    f"Attempt limit ({MAX_ATTEMPTS}) reached — similar title "
                    f"({ratio:.0%} match): '{story.title}' ≈ '{seen_title}'"
                )

    logger.debug("Story is not a duplicate: %s", story.title)


def _title_words(normalised: str) -> set[str]:
    return {word for word in normalised.split() if len(word) >= 4}


def _build_title_index(seen: dict) -> dict:
    """Build a cheap word index before the slower fuzzy title comparison."""
    result: dict = {}
    for key, titles in (
        ("permanent", seen.get("titles", [])),
        ("attempts", seen.get("title_attempts", {}).keys()),
    ):
        rows = [(title, _normalise(title)) for title in titles]
        words: dict[str, set[int]] = defaultdict(set)
        for idx, (_, normalised) in enumerate(rows):
            for word in _title_words(normalised):
                words[word].add(idx)
        result[key] = {"rows": rows, "words": words}
    return result


def _candidate_titles(normalised: str, index: dict, key: str):
    bucket = index[key]
    rows = bucket["rows"]
    words = _title_words(normalised)
    if not words:
        return rows
    candidate_ids: set[int] = set()
    for word in words:
        candidate_ids.update(bucket["words"].get(word, ()))
    return [rows[idx] for idx in candidate_ids]


def mark_seen(story: Story, permanent: bool = False) -> None:
    """
    Record this story in seen_stories.json.

    permanent=True  → blocks forever (call after successful publish).
    permanent=False → increments attempt counter; blocked after MAX_ATTEMPTS.
                      Entry expires after TTL_DAYS if never published.
    """
    seen = _load_seen()
    now_iso = datetime.now(timezone.utc).isoformat()
    _record_seen(seen, story, permanent=permanent, now_iso=now_iso)
    _save_seen(seen)


def _record_seen(seen: dict, story: Story, permanent: bool, now_iso: str) -> None:
    """Update an already-loaded state mapping without performing disk I/O."""

    if permanent:
        # Move to permanent lists — never pruned
        if story.source_url not in seen.get("urls", []):
            seen.setdefault("urls", []).append(story.source_url)
        if story.title not in seen.get("titles", []):
            seen.setdefault("titles", []).append(story.title)
        # Clean up attempt entries if they exist
        seen.get("url_attempts", {}).pop(story.source_url, None)
        seen.get("title_attempts", {}).pop(story.title, None)
        logger.debug("Permanently marked as seen: %s", story.title)
    else:
        # Increment attempt counters with timestamp
        url_attempts = seen.setdefault("url_attempts", {})
        if story.source_url in url_attempts:
            url_attempts[story.source_url]["count"] += 1
            url_attempts[story.source_url]["last_seen"] = now_iso
        else:
            url_attempts[story.source_url] = {"count": 1, "first_seen": now_iso, "last_seen": now_iso}

        title_attempts = seen.setdefault("title_attempts", {})
        if story.title in title_attempts:
            title_attempts[story.title]["count"] += 1
            title_attempts[story.title]["last_seen"] = now_iso
        else:
            title_attempts[story.title] = {"count": 1, "first_seen": now_iso, "last_seen": now_iso}

        count = url_attempts[story.source_url]["count"]
        logger.debug("Marked as seen (attempt %d/%d): %s", count, MAX_ATTEMPTS, story.title)



# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_seen() -> dict:
    path = Path(config.SEEN_STORIES_PATH)
    if not path.exists():
        return {"urls": [], "titles": [], "url_attempts": {}, "title_attempts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            # Legacy format — migrate
            return {"urls": [], "titles": [], "url_attempts": {}, "title_attempts": {}}
        return _prune(data)
    except (json.JSONDecodeError, OSError):
        return {"urls": [], "titles": [], "url_attempts": {}, "title_attempts": {}}


def _prune(seen: dict) -> dict:
    """Remove attempt-tracked entries older than TTL_DAYS. Permanent entries are untouched."""
    now = datetime.now(timezone.utc)
    cutoff_days = TTL_DAYS

    def is_expired(entry: dict) -> bool:
        try:
            last = datetime.fromisoformat(entry.get("last_seen", ""))
            return (now - last).days > cutoff_days
        except (ValueError, TypeError):
            return False  # Keep entries with missing/invalid timestamps

    before_url   = len(seen.get("url_attempts", {}))
    before_title = len(seen.get("title_attempts", {}))

    seen["url_attempts"]   = {k: v for k, v in seen.get("url_attempts", {}).items()   if not is_expired(v)}
    seen["title_attempts"] = {k: v for k, v in seen.get("title_attempts", {}).items() if not is_expired(v)}

    pruned = (before_url - len(seen["url_attempts"])) + (before_title - len(seen["title_attempts"]))
    if pruned:
        logger.debug("Pruned %d expired entries from seen_stories (TTL=%d days)", pruned, cutoff_days)

    return seen


def _save_seen(seen: dict) -> None:
    path = Path(config.SEEN_STORIES_PATH)
    path.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalise(title: str) -> str:
    """Lowercase and strip punctuation for more reliable fuzzy comparison."""
    return "".join(c.lower() for c in title if c.isalnum() or c.isspace()).strip()
