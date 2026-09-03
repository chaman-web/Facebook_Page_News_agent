"""
pipeline/selector.py — Select the best story candidate from a list.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from models import Story, StoryRejected

logger = logging.getLogger(__name__)


def select_story(stories: list[Story]) -> Story:
    """
    Return the best story from the list.

    Criteria (in order):
    1. Has a title and URL.
    2. Not older than NEWS_MAX_AGE_HOURS.
    3. Has a non-empty summary.

    Falls back to accepting stories without summaries if nothing better is found.
    Raises StoryRejected if no acceptable story exists.
    """
    now = datetime.now(timezone.utc)
    max_age_seconds = config.NEWS_MAX_AGE_HOURS * 3600

    acceptable: list[Story] = []
    fallback: list[Story] = []  # Stories that pass basic checks but lack summaries

    for story in stories:
        if not story.title or not story.source_url:
            continue
        if "[Removed]" in story.title:
            continue

        age_seconds = (now - story.published_at.replace(tzinfo=timezone.utc)
                       if story.published_at.tzinfo is None
                       else (now - story.published_at)).total_seconds()

        if age_seconds > max_age_seconds:
            logger.debug("Skipping old story (%dh): %s", age_seconds // 3600, story.title)
            continue

        if story.raw_summary:
            acceptable.append(story)
        else:
            fallback.append(story)

    candidates = acceptable or fallback

    if not candidates:
        raise StoryRejected(
            f"No acceptable stories found within the last {config.NEWS_MAX_AGE_HOURS} hours."
        )

    selected = candidates[0]
    logger.info("Selected story: %s [%s]", selected.title, selected.source_name)
    return selected
