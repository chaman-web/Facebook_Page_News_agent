from datetime import datetime, timezone

from models import Story
from pipeline.story_topic import card_topic


def _story(title: str, summary: str = "", category: str = "world") -> Story:
    return Story(
        title=title,
        source_name="BBC News",
        source_url="https://bbc.example/story",
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
        category=category,
    )


def test_crime_story_is_not_labelled_world_news():
    story = _story("Man accused of triple murder admits gun charges in South Africa")
    assert card_topic(story) == "crime"


def test_sanctions_story_uses_politics_label():
    story = _story("UK imposes sanctions on Israeli settlements", "The measures mark a government policy shift.")
    assert card_topic(story) == "politics"


def test_tariff_story_uses_business_label():
    story = _story("Canada imposes tariffs on US goods", category="breaking")
    assert card_topic(story) == "business"


def test_existing_specific_category_is_safe_fallback():
    assert card_topic(_story("Local team announces a new captain", category="sports")) == "sports"
