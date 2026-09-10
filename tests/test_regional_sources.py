from datetime import datetime, timedelta, timezone

from models import Story
from unittest.mock import patch

from news.fetcher import fetch_regional_news
from news.regional_sources import (
    REGIONAL_FEEDS,
    assess_regional_impact,
    discovery_priority,
    is_high_impact_candidate,
    is_region_relevant,
)
from pipeline.source_classifier import SourceTier, classify_source


def _story(title: str, age_minutes: int) -> Story:
    return Story(
        title=title,
        source_name="Test Source",
        source_url=f"https://example.com/{age_minutes}",
        published_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        raw_summary="",
    )


def test_regional_discovery_prioritizes_impact_over_soft_freshness():
    soft = _story("Ten films to watch this weekend", 1)
    impact = _story("Major earthquake emergency leaves hundreds missing", 30)

    assert discovery_priority(impact) > discovery_priority(soft)
    assert is_high_impact_candidate(impact)
    assert not is_high_impact_candidate(soft)


def test_story_carries_region_without_changing_default_behavior():
    story = _story("Routine headline", 1)
    assert story.region == "global"
    story.region = "pakistan"
    assert story.region == "pakistan"


def test_major_regional_consequences_are_measured_across_dimensions():
    story = _story(
        "Pakistan declares nationwide state of emergency after blackout disrupts hospitals",
        2,
    )
    story.region = "pakistan"

    impact = assess_regional_impact(story)

    assert impact.is_major
    assert impact.score >= 8
    assert {"emergency-response", "essential-services", "national-reach"} <= set(impact.reasons)


def test_global_story_does_not_receive_regional_impact_bonus():
    story = _story(
        "Nationwide state of emergency after blackout disrupts hospitals",
        2,
    )

    impact = assess_regional_impact(story)

    assert impact.score == 0
    assert impact.reasons == ()


def test_multilingual_regional_signals_protect_local_candidate():
    story = _story("پاکستان میں ہنگامی حالت، بجلی بند", 2)
    story.region = "pakistan"

    assert is_region_relevant(story, "pakistan")
    assert assess_regional_impact(story).is_major
    assert is_high_impact_candidate(story)


def test_regional_relevance_rejects_global_story_from_local_publisher():
    iran_story = _story("Three attacks reported in Tehran", 1)
    australia_story = _story("Australian government announces nationwide policy", 1)

    assert not is_region_relevant(iran_story, "oceania")
    assert is_region_relevant(australia_story, "oceania")


def test_every_world_region_has_multiple_discovery_sources():
    expected = {
        "pakistan", "india", "south_asia", "gulf_ksa_uae", "middle_east",
        "turkey", "europe", "east_southeast_asia", "africa",
        "north_america", "latin_america", "oceania", "official_global",
    }
    assert expected <= REGIONAL_FEEDS.keys()
    assert all(len(REGIONAL_FEEDS[region]) >= 2 for region in expected)


def test_additional_high_impact_regional_candidates_bypass_sample_limit():
    first = _story("Pakistan declares earthquake emergency", 1)
    first.source_url = "https://local.test/earthquake"
    second = _story("Pakistan election court issues major ruling", 2)
    second.source_url = "https://local.test/election"
    routine = _story("Pakistan arts festival opens this weekend", 3)
    routine.source_url = "https://local.test/arts"

    with (
        patch("news.fetcher.REGIONAL_FEEDS", {"pakistan": ("https://feed.test/rss",)}),
        patch("news.fetcher.is_banned", return_value=False),
        patch("news.fetcher._fetch_feed_resilient", return_value=[first, second, routine]),
    ):
        selected = fetch_regional_news(limit_per_region=1)

    assert {story.source_url for story in selected} == {
        first.source_url,
        second.source_url,
    }


def test_expired_high_impact_story_is_excluded_from_regional_discovery():
    expired = _story("Pakistan declares national emergency", 49 * 60)
    expired.source_url = "https://local.test/expired-emergency"

    with (
        patch("news.fetcher.REGIONAL_FEEDS", {"pakistan": ("https://feed.test/rss",)}),
        patch("news.fetcher.is_banned", return_value=False),
        patch("news.fetcher._fetch_feed_resilient", return_value=[expired]),
    ):
        selected = fetch_regional_news(limit_per_region=1)

    assert selected == []


def test_new_local_publishers_are_classified_as_established_sources():
    for url in (
        "https://arynews.tv/feed/",
        "https://indianexpress.com/feed/",
        "https://www.thenationalnews.com/world/",
        "https://www.koreaherald.com/world",
        "https://www.premiumtimesng.com/news",
        "https://buenosairesherald.com/world",
        "https://www.rnz.co.nz/news",
    ):
        assert classify_source("Regional News", url).tier == SourceTier.TIER2
