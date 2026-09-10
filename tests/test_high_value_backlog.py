from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from models import Story
from pipeline.high_value_backlog import _load, pending_stories, remember, resolve


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


def test_high_value_story_records_latest_rescue_stage_and_reason(tmp_path):
    with patch("config.HIGH_VALUE_BACKLOG_PATH", tmp_path / "backlog.json"):
        story = _story()
        remember(
            story,
            88,
            10,
            stage="verification",
            reason="Only one independent source is currently available.",
            impact_reasons=("major-disaster", "human-safety"),
        )

        record = _load()[story.source_url]

        assert record["last_stage"] == "verification"
        assert record["last_reason"] == "Only one independent source is currently available."
        assert record["impact_reasons"] == ["major-disaster", "human-safety"]


def test_rescue_update_preserves_highest_known_scores(tmp_path):
    with patch("config.HIGH_VALUE_BACKLOG_PATH", tmp_path / "backlog.json"):
        story = _story()
        remember(story, 91, 15, impact_reasons=("major-disaster",))
        remember(story, 0, 10, stage="content_hold", reason="Awaiting stronger summary.")

        record = _load()[story.source_url]

        assert record["score"] == 91
        assert record["impact_score"] == 15
        assert record["impact_reasons"] == ["major-disaster"]
