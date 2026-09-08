import json
from unittest.mock import patch

from pipeline import source_health


def test_transient_failure_does_not_immediately_ban_source(tmp_path):
    health_file = tmp_path / "health.json"
    with patch.object(source_health, "HEALTH_FILE", health_file):
        source_health.record_failure("https://feed.example/rss", "timeout")
        state = json.loads(health_file.read_text(encoding="utf-8"))

        assert state["https://feed.example/rss"]["consecutive_failures"] == 1
        assert not source_health.is_banned("https://feed.example/rss")


def test_source_cools_off_only_after_three_consecutive_failures(tmp_path):
    health_file = tmp_path / "health.json"
    url = "https://feed.example/rss"
    with patch.object(source_health, "HEALTH_FILE", health_file):
        for _ in range(3):
            source_health.record_failure(url, "timeout")

        assert source_health.is_banned(url)
        source_health.record_success(url)
        assert not source_health.is_banned(url)
