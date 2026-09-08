from datetime import datetime, timezone

from models import Story
from pipeline.card_headline import select_card_headline


def _story() -> Story:
    return Story(
        title="Earthquake kills 12 people in Turkey",
        source_name="BBC",
        source_url="https://bbc.com/example",
        published_at=datetime.now(timezone.utc),
        raw_summary="A powerful earthquake killed 12 people in Turkey and damaged buildings.",
    )


def test_selects_grounded_specific_headline():
    raw = """CARD_HEADLINES:
1. YOU WON'T BELIEVE WHAT HAPPENED NEXT
2. TURKEY EARTHQUAKE KILLS 500 PEOPLE
3. TURKEY EARTHQUAKE KILLS 12 PEOPLE

POST:
Body
"""
    selected = select_card_headline(_story(), raw)
    assert "12" in selected
    assert "500" not in selected
    assert "BELIEVE" not in selected


def test_rejects_vague_option_in_favor_of_clear_action():
    raw = """CARD_HEADLINES:
1. MAJOR DEVELOPMENTS CHANGE THE SITUATION
2. EARTHQUAKE KILLS 12 PEOPLE IN TURKEY
3. WHAT HAPPENS NEXT IN TURKEY?

POST:
Body
"""
    assert select_card_headline(_story(), raw) == "EARTHQUAKE KILLS 12 PEOPLE IN TURKEY"


def test_fallback_remains_short_and_complete():
    headline = select_card_headline(_story(), "POST:\nBody")
    assert len(headline.split()) <= 10
    assert not headline.endswith((" IN", " OF", " TO", " THE"))
