from datetime import datetime, timezone

from models import Story
from pipeline.generator import _format_post
from facebook.publisher import _format_post as format_for_facebook


def test_opening_matches_card_and_uses_one_leading_symbol():
    story = Story(
        title="Earthquake kills 12 people in Turkey",
        source_name="BBC",
        source_url="https://bbc.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="The earthquake damaged buildings across southern Turkey.",
        category="world",
        card_headline="TURKEY EARTHQUAKE KILLS 12 PEOPLE",
    )
    post = """🌍 WORLD: Earthquake kills 12 people in Turkey
Rescue teams are searching damaged buildings across southern Turkey.

Officials said emergency crews remain at the scene.

📰 Sources: BBC"""

    result = _format_post(post, story)
    lines = result.splitlines()

    assert lines[0] == "🌍 TURKEY EARTHQUAKE KILLS 12 PEOPLE"
    assert lines[1] == "Why this matters: Rescue teams are searching damaged buildings across southern Turkey."
    assert lines[2] == ""
    assert result.count("🌍") == 1
    assert result.count("Rescue teams are searching damaged buildings across southern Turkey.") == 1
    assert "What do you think" not in result
    assert "👇" not in result


def test_story_specific_question_is_preserved_without_engagement_prompt():
    story = Story(
        title="City approves a new transit plan",
        source_name="Reuters",
        source_url="https://reuters.com/transit",
        published_at=datetime.now(timezone.utc),
        raw_summary="The plan changes bus routes across the city.",
        category="world",
        card_headline="CITY APPROVES NEW TRANSIT PLAN",
    )
    post = """🌍 City approves a new transit plan
The plan changes bus routes across the city.

Which neighborhoods will receive the first new routes?

📰 Sources: Reuters"""

    result = _format_post(post, story)

    assert "Which neighborhoods will receive the first new routes?" in result
    assert "comment below" not in result.lower()


def test_publisher_preserves_two_line_hook_without_breaking_override():
    text = "🌍 TURKEY EARTHQUAKE KILLS 12 PEOPLE\nRescue teams search damaged buildings.\n\nMore details."
    result = format_for_facebook(text, ["#WorldNews"])

    assert result.startswith("🌍 TURKEY EARTHQUAKE KILLS 12 PEOPLE\nRescue teams")
    assert not result.startswith("BREAKING:")
