from datetime import datetime, timedelta, timezone

from models import Story
from news.regional_sources import discovery_priority, is_region_relevant


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


def test_story_carries_region_without_changing_default_behavior():
    story = _story("Routine headline", 1)
    assert story.region == "global"
    story.region = "pakistan"
    assert story.region == "pakistan"


def test_regional_relevance_rejects_global_story_from_local_publisher():
    iran_story = _story("Three attacks reported in Tehran", 1)
    australia_story = _story("Australian government announces nationwide policy", 1)

    assert not is_region_relevant(iran_story, "oceania")
    assert is_region_relevant(australia_story, "oceania")
