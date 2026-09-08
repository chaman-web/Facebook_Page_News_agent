from datetime import datetime, timezone

from models import Story
from pipeline.card_description import select_card_description


def _story() -> Story:
    return Story(
        title="UK imposes sanctions on West Bank settlements",
        source_name="New York Times",
        source_url="https://nytimes.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary=(
            "The measures are expected to include a trade ban on settlement goods. "
            "The move marks a major shift in British policy toward Israel."
        ),
    )


def test_selects_relevant_distinct_grounded_description():
    raw = (
        "CARD_DESCRIPTION:\n"
        "A trade ban on settlement goods marks a major shift in British policy.\n"
        "POST:\nBody"
    )
    assert select_card_description(_story(), raw) == (
        "A trade ban on settlement goods marks a major shift in British policy."
    )


def test_rejects_title_repetition():
    raw = "CARD_DESCRIPTION:\nUK imposes sanctions on West Bank settlements today.\nPOST:\nBody"
    assert select_card_description(_story(), raw) is None


def test_rejects_unrelated_or_unsupported_description():
    raw = "CARD_DESCRIPTION:\nMarkets in Japan suffered their largest crash in decades.\nPOST:\nBody"
    assert select_card_description(_story(), raw) is None
