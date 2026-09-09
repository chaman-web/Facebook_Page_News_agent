from datetime import datetime, timezone

import pytest

from models import Story
from pipeline import final_quality_check as quality


def _story(content: str) -> Story:
    story = Story(
        title="Major verified public update",
        source_name="Example News",
        source_url="https://example.com/update",
        category="world",
        published_at=datetime.now(timezone.utc),
        raw_summary="A sufficiently detailed verified public update.",
    )
    story.post_content = content
    return story


def test_preflight_does_not_remember_failed_delivery_candidate():
    quality._RECENT_POSTS.clear()
    post = "📌 A verified major public update has been announced with details for residents.\n\nSources: Example News"

    quality.final_quality_check(_story(post), remember=False)
    quality.final_quality_check(_story(post), remember=False)

    assert quality._RECENT_POSTS == []


def test_successful_delivery_memory_blocks_duplicate():
    quality._RECENT_POSTS.clear()
    post = "📌 A verified major public update has been announced with details for residents.\n\nSources: Example News"
    quality.remember_post(post)

    with pytest.raises(quality.FinalQualityError, match="too similar"):
        quality.final_quality_check(_story(post), remember=False)

    quality._RECENT_POSTS.clear()
