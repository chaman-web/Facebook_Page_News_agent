from datetime import datetime, timezone

from models import Story
from pipeline.editorial_scorer import EditorialTier, assess_high_impact, score_story


def _story(title: str, summary: str, category: str = "world") -> Story:
    return Story(
        title=title,
        source_name="Established News",
        source_url="https://bbc.com/news/example",
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
        source_tier=2,
        category=category,
    )


def test_mass_casualty_disaster_gets_high_impact_protection():
    story = _story(
        "Major earthquake kills 120 people",
        "A national emergency was declared after the earthquake destroyed several cities.",
        category="science",
    )

    score = score_story(story)

    assert score.impact_score >= 10
    assert "mass-casualty" in score.impact_reasons
    assert "major-disaster" in score.impact_reasons
    assert score.total >= 82
    assert score.tier in {EditorialTier.HIGH, EditorialTier.PRIORITY}
    assert score.effective_tier_num == 1


def test_routine_soft_story_does_not_receive_impact_floor():
    story = _story(
        "Ten films to watch this weekend",
        "A critic shares a personal selection of recent releases.",
        category="entertainment",
    )

    score = score_story(story)

    assert assess_high_impact(f"{story.title} {story.raw_summary}")[0] == 0
    assert score.impact_score == 0
    assert score.effective_tier_num == 3


def test_shadow_reduces_keyword_authority_without_losing_high_impact_floor():
    story = _story(
        "Breaking urgent war attack missile crisis",
        "A missile strike left 120 people killed during a conflict escalation.",
        category="technology",
    )

    score = score_story(story)

    assert score.shadow_total <= score.total
    assert score.impact_score >= 10
    assert score.shadow_total >= 82
    assert score.shadow_tier in {EditorialTier.HIGH, EditorialTier.PRIORITY}
