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
from pipeline.deduplicator import (  # noqa: E402
    canonical_story_url,
    check_duplicate,
    filter_fresh_stories,
    mark_seen,
)


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

    def test_tracking_and_scheme_url_variant_raises(self, tmp_path):
        seen_path = _write_seen(
            tmp_path,
            {"urls": ["http://www.example.com/story-1?utm_source=facebook"], "titles": []},
        )
        story = _story("Same report from another link", "https://example.com/story-1?fbclid=abc")

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

    def test_protected_story_ignores_attempt_limit(self, tmp_path):
        story = _story("Major emergency update", "https://example.com/high")
        story.priority_protected = True
        seen_path = _write_seen(tmp_path, {
            "urls": [], "titles": [],
            "url_attempts": {story.source_url: {"count": 9}},
            "title_attempts": {story.title: {"count": 9}},
        })

        with patch("config.SEEN_STORIES_PATH", str(seen_path)):
            check_duplicate(story)

    def test_material_numeric_update_passes_similar_title_history(self, tmp_path):
        seen_path = _write_seen(tmp_path, {
            "urls": [],
            "titles": ["Earthquake death toll rises to 20 after rescue operation"],
        })
        story = _story(
            "Earthquake death toll rises to 40 after rescue operation",
            "https://example.com/earthquake-update",
        )
        with patch("config.SEEN_STORIES_PATH", str(seen_path)):
            check_duplicate(story)


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


def test_batch_dedup_blocks_same_story_from_two_sources(tmp_path):
    seen_path = _write_seen(
        tmp_path,
        {"urls": [], "titles": [], "url_attempts": {}, "title_attempts": {}},
    )
    stories = [
        _story("Major earthquake strikes coastal region", "https://one.example/report"),
        _story("Major earthquake strikes coastal region", "https://two.example/report"),
    ]

    with patch("config.SEEN_STORIES_PATH", str(seen_path)):
        fresh, duplicates = filter_fresh_stories(stories)

    assert [story.source_url for story in fresh] == ["https://one.example/report"]
    assert duplicates == 1


def test_batch_dedup_reports_exact_reason_to_optional_audit_callback(tmp_path):
    seen_path = _write_seen(
        tmp_path,
        {"urls": [], "titles": [], "url_attempts": {}, "title_attempts": {}},
    )
    stories = [
        _story("Major earthquake strikes coastal region", "https://one.example/report"),
        _story("Major earthquake strikes coastal region", "https://two.example/report"),
    ]
    audited: list[tuple[str, str]] = []

    with patch("config.SEEN_STORIES_PATH", str(seen_path)):
        filter_fresh_stories(
            stories,
            on_duplicate=lambda story, reason: audited.append((story.source_url, reason)),
        )

    assert audited[0][0] == "https://two.example/report"
    assert "Duplicate within current fetch" in audited[0][1]


def test_batch_dedup_blocks_tracking_variant_with_different_title(tmp_path):
    seen_path = _write_seen(
        tmp_path,
        {"urls": [], "titles": [], "url_attempts": {}, "title_attempts": {}},
    )
    stories = [
        _story("Original report headline", "http://www.example.com/report?utm_source=rss"),
        _story("Publisher rewrites its headline", "https://example.com/report?fbclid=abc"),
    ]

    with patch("config.SEEN_STORIES_PATH", str(seen_path)):
        fresh, duplicates = filter_fresh_stories(stories)

    assert [story.title for story in fresh] == ["Original report headline"]
    assert duplicates == 1


def test_canonical_story_url_removes_only_delivery_tracking_noise():
    assert canonical_story_url(
        "http://www.Example.com/news/item/?id=7&utm_medium=social&fbclid=abc#comments"
    ) == "https://example.com/news/item?id=7"
