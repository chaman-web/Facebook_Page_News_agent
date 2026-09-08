from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from models import Story
from pipeline.high_value_backlog import pending_stories, remember, resolve


def _story(age_hours: int = 0) -> Story:
    return Story(
        title="Major national emergency",
        source_name="BBC",
        source_url="https://bbc.com/high",
        published_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        raw_summary="A national emergency affects millions.",
    )


def test_high_value_story_survives_until_resolved(tmp_path):
    with patch("config.HIGH_VALUE_BACKLOG_PATH", tmp_path / "backlog.json"):
        story = _story()
        remember(story, 91, 15)
        restored = pending_stories()
        assert restored[0].priority_protected is True
        resolve(story.source_url)
        assert pending_stories() == []


def test_expired_high_value_story_is_pruned(tmp_path):
    with patch("config.HIGH_VALUE_BACKLOG_PATH", tmp_path / "backlog.json"):
        remember(_story(age_hours=60), 90, 15)
        assert pending_stories(max_age_hours=48) == []
