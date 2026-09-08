from datetime import datetime, timezone

from models import Story
from pipeline.post_fact_checker import check_generated_facts


def _story() -> Story:
    return Story(
        title="Russia and North Korea open first road bridge",
        source_name="BBC News",
        source_url="https://bbc.example/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="The bridge crosses the Tumen River between Rason and Khasan.",
        article_text="The one-kilometre bridge will initially carry cargo. Passenger traffic is expected later.",
        corroborating_sources=[{"title": "First road bridge links Russia and North Korea"}],
    )


def test_rejects_unsupported_number_and_location_relation():
    result = check_generated_facts(
        _story(),
        "Russia and North Korea opened a 48-kilometre road to Ukraine. The route will carry cargo.",
    )
    assert not result.passed
    assert any("unsupported number" in issue for issue in result.issues)
    assert any("to Ukraine" in issue for issue in result.issues)


def test_accepts_grounded_caption():
    result = check_generated_facts(
        _story(),
        "Russia and North Korea opened their first road bridge. The bridge crosses the Tumen River between Rason and Khasan.",
    )
    assert result.passed


def test_accepts_known_location_with_different_preposition():
    result = check_generated_facts(
        _story(),
        "Cargo could travel to North Korea across the first road bridge.",
    )
    assert result.passed
