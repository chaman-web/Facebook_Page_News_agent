"""
tests/test_deduplicator.py — Unit tests for the duplicate detection logic.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Minimal environment so config.py doesn't exit during import
# ---------------------------------------------------------------------------
os.environ.setdefault("NEWSAPI_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from models import DuplicateStory, Story  # noqa: E402
from pipeline.deduplicator import check_duplicate, filter_fresh_stories, mark_seen  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _story(title: str, url: str) -> Story:
    return Story(
        title=title,
        source_name="Test Source",
        source_url=url,
        published_at=datetime.now(timezone.utc),
        raw_summary="Test summary.",
    )


def _write_seen(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "seen_stories.json"
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCheckDuplicate:

    def test_exact_url_match_raises(self, tmp_path):
        seen_path = _write_seen(
            tmp_path,
            {"urls": ["https://example.com/story-1"], "titles": ["Some Story"]},
        )
        story = _story("New Story", "https://example.com/story-1")

        with patch("config.SEEN_STORIES_PATH", str(seen_path)):
            with pytest.raises(DuplicateStory, match="Duplicate URL"):
                check_duplicate(story)

    def test_similar_title_raises(self, tmp_path):
        seen_path = _write_seen(
            tmp_path,
            {
                "urls": [],
                "titles": ["President Signs New Climate Agreement"],
            },
        )
        # Very similar title — should exceed 85% threshold
        story = _story("President Signs New Climate Agreement Today", "https://example.com/new")

        with patch("config.SEEN_STORIES_PATH", str(seen_path)):
            with pytest.raises(DuplicateStory, match="Similar title"):
                check_duplicate(story)

    def test_unrelated_story_passes(self, tmp_path):
        seen_path = _write_seen(
            tmp_path,
            {
                "urls": ["https://example.com/other"],
                "titles": ["Earthquake Strikes Japan"],
            },
        )
        story = _story("Stock Markets Rally on Fed Decision", "https://example.com/stocks")

        with patch("config.SEEN_STORIES_PATH", str(seen_path)):
            # Should not raise
            check_duplicate(story)

    def test_empty_seen_file_passes(self, tmp_path):
        seen_path = _write_seen(tmp_path, {"urls": [], "titles": []})
        story = _story("Breaking: Something Happened", "https://example.com/breaking")

        with patch("config.SEEN_STORIES_PATH", str(seen_path)):
            check_duplicate(story)  # Must not raise


class TestMarkSeen:

    def test_permanent_mark_seen_appends_url_and_title(self, tmp_path):
        seen_path = _write_seen(tmp_path, {"urls": [], "titles": []})
        story = _story("Major Summit Begins in Geneva", "https://example.com/summit")

        with patch("config.SEEN_STORIES_PATH", str(seen_path)):
            mark_seen(story, permanent=True)
            data = json.loads(seen_path.read_text())

        assert "https://example.com/summit" in data["urls"]
        assert "Major Summit Begins in Geneva" in data["titles"]


def test_batch_dedup_reads_and_writes_state_once(tmp_path):
    seen_path = _write_seen(
        tmp_path,
        {"urls": [], "titles": [], "url_attempts": {}, "title_attempts": {}},
    )
    stories = [
        _story(f"Distinct story number {i}", f"https://example.com/{i}")
        for i in range(10)
    ]
    with patch("config.SEEN_STORIES_PATH", str(seen_path)), \
         patch("pipeline.deduplicator._save_seen", wraps=__import__("pipeline.deduplicator", fromlist=["_save_seen"])._save_seen) as save:
        fresh, duplicates = filter_fresh_stories(stories)

    assert len(fresh) == 10
    assert duplicates == 0
    assert save.call_count == 1
