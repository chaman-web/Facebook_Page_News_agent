import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from models import Story
from news import fetcher
from pipeline import source_health


def test_transient_failure_does_not_immediately_ban_source(tmp_path):
    health_file = tmp_path / "health.json"
    with patch.object(source_health, "HEALTH_FILE", health_file):
        source_health.record_failure("https://feed.example/rss", "timeout")
        state = json.loads(health_file.read_text(encoding="utf-8"))

        assert state["https://feed.example/rss"]["consecutive_failures"] == 1
        assert not source_health.is_banned("https://feed.example/rss")


def test_source_cools_off_only_after_repeated_consecutive_failures(tmp_path):
    health_file = tmp_path / "health.json"
    url = "https://feed.example/rss"
    with patch.object(source_health, "HEALTH_FILE", health_file):
        for _ in range(source_health.FAILURE_THRESHOLD):
            source_health.record_failure(url, "timeout")

        assert source_health.is_banned(url)
        source_health.record_success(url)
        assert not source_health.is_banned(url)


def test_important_source_receives_an_extra_attempt(tmp_path):
    health_file = tmp_path / "health.json"
    url = "https://feeds.reuters.com/reuters/topNews"
    with patch.object(source_health, "HEALTH_FILE", health_file):
        for _ in range(source_health.FAILURE_THRESHOLD):
            source_health.record_failure(url, "timeout")
        assert not source_health.is_banned(url)

        source_health.record_failure(url, "timeout")
        assert source_health.is_banned(url)


def test_cooling_endpoint_gets_automatic_recovery_probe(tmp_path):
    health_file = tmp_path / "health.json"
    url = "https://feed.example/rss"
    started = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
    with (
        patch.object(source_health, "HEALTH_FILE", health_file),
        patch.object(source_health, "_utcnow", return_value=started),
    ):
        for _ in range(source_health.FAILURE_THRESHOLD):
            source_health.record_failure(url, "timeout")
        assert source_health.is_banned(url)

    with (
        patch.object(source_health, "HEALTH_FILE", health_file),
        patch.object(source_health, "_utcnow", return_value=started + timedelta(minutes=5)),
    ):
        assert not source_health.is_banned(url)
        source_health.record_success(url)
        assert not source_health.is_banned(url)


def test_failure_of_one_endpoint_does_not_cool_same_publisher(tmp_path):
    health_file = tmp_path / "health.json"
    failed = "https://example.com/rss/world"
    alternative = "https://example.com/rss/latest"
    with patch.object(source_health, "HEALTH_FILE", health_file):
        for _ in range(source_health.FAILURE_THRESHOLD):
            source_health.record_failure(failed, "connection error")

        assert source_health.is_banned(failed)
        assert not source_health.is_banned(alternative)


def test_duplicate_heavy_feed_is_recorded_without_cooling(tmp_path):
    health_file = tmp_path / "health.json"
    url = "https://feed.example/rss"
    with patch.object(source_health, "HEALTH_FILE", health_file):
        source_health.record_duplicates(url, total=10, duplicates=9)
        state = json.loads(health_file.read_text(encoding="utf-8"))

        assert state[url]["last_duplicate_ratio"] == 0.9
        assert not source_health.is_banned(url)


def test_transient_rss_request_is_retried_once():
    response = Mock()
    response.content = b"""
        <rss><channel><title>Example News</title><item>
        <title>Important update</title><link>https://example.com/story</link>
        <description>Verified details.</description>
        </item></channel></rss>
    """
    response.raise_for_status.return_value = None

    with (
        patch("news.fetcher.requests.get", side_effect=[requests.ConnectionError("temporary"), response]) as get,
        patch("news.fetcher.time.sleep") as sleep,
    ):
        stories = fetcher._parse_rss_feed("https://feed.example/rss", limit=10)

    assert len(stories) == 1
    assert get.call_count == 2
    sleep.assert_called_once()


def test_recent_feed_cache_covers_temporary_outage(tmp_path):
    url = "https://feed.example/rss"
    story = Story(
        title="Important regional update",
        source_name="Example News",
        source_url="https://example.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="Recent verified details remain available during a short outage.",
    )

    with (
        patch.object(fetcher, "_FEED_CACHE_DIR", tmp_path),
        patch.object(fetcher, "_parse_rss_feed", return_value=[story]),
        patch.object(fetcher, "record_success"),
    ):
        assert fetcher._fetch_feed_resilient(url, limit=10, context="Fetch") == [story]

    with (
        patch.object(fetcher, "_FEED_CACHE_DIR", tmp_path),
        patch.object(fetcher, "_parse_rss_feed", side_effect=requests.ConnectionError("offline")),
        patch.object(fetcher, "record_failure") as failure,
    ):
        cached = fetcher._fetch_feed_resilient(url, limit=10, context="Fetch")

    assert [item.source_url for item in cached] == [story.source_url]
    failure.assert_called_once()


def test_shared_feed_is_fetched_only_once_per_job():
    shared_url = "https://feed.example/rss"
    story = Story(
        title="Shared source update",
        source_name="Example News",
        source_url="https://example.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="One endpoint can serve more than one discovery lane.",
    )
    run_cache = {shared_url: [story]}

    with (
        patch.object(fetcher, "REGIONAL_FEEDS", {"official_global": (shared_url,)}),
        patch.object(fetcher, "_fetch_feed_resilient") as fetch,
    ):
        stories = fetcher.fetch_regional_news(limit_per_region=1, feed_cache=run_cache)

    assert [item.source_url for item in stories] == [story.source_url]
    fetch.assert_not_called()


def test_repeated_failure_warnings_are_grouped(tmp_path, caplog):
    health_file = tmp_path / "health.json"
    url = "https://feed.example/rss"
    now = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
    with (
        patch.object(source_health, "HEALTH_FILE", health_file),
        patch.object(source_health, "_utcnow", return_value=now),
        caplog.at_level("WARNING"),
    ):
        source_health.record_failure(url, "connection error")
        source_health.record_failure(url, "connection error")

    warnings = [record.message for record in caplog.records if "endpoint failure" in record.message]
    assert len(warnings) == 1
    state = json.loads(health_file.read_text(encoding="utf-8"))
    assert state[url]["suppressed_failure_logs"] == 1
