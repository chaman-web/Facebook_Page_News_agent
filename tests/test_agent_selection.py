from datetime import datetime, timezone
from types import SimpleNamespace

from agent import _preserve_high_impact
from models import Story


def _item(url: str, *, total: float, impact: float):
    story = Story(
        title=url,
        source_name="Test Source",
        source_url=url,
        published_at=datetime.now(timezone.utc),
        raw_summary="Test summary",
    )
    score = SimpleNamespace(total=total, impact_score=impact)
    return score, story


def test_tier_one_selection_preserves_high_value_and_high_impact_candidates():
    high_value = _item("https://example.com/value", total=84, impact=0)
    high_impact = _item("https://example.com/impact", total=72, impact=10)
    moderate = _item("https://example.com/moderate", total=72, impact=0)

    selected = _preserve_high_impact(
        [high_value, high_impact, moderate],
        [moderate],
    )

    assert selected == [moderate, high_value, high_impact]
