from datetime import datetime, timezone
from unittest.mock import Mock, patch

from models import Story
from pipeline.generator import (
    _grounded_fallback_post,
    generate_post,
    reset_generation_backend_state,
)
from pipeline.post_fact_checker import check_generated_facts


def test_fallback_uses_verified_evidence_and_passes_fact_check():
    story = Story(
        title="Man accused of triple murder in UK admits gun charges in South Africa",
        source_name="BBC News",
        source_url="https://bbc.co.uk/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="A man appeared in court in South Africa after his family were found dead in the UK.",
        article_text=(
            "Prosecutors authorised murder charges against the defendant. "
            "An extradition hearing is due to take place in South Africa."
        ),
        card_headline="MAN ADMITS GUN CHARGES IN SOUTH AFRICA",
    )
    post = _grounded_fallback_post(story)
    assert "BBC News" in post
    assert "Why this matters:" in post
    assert "👇" not in post
    assert check_generated_facts(story, post).passed


def test_backend_failure_uses_grounded_fallback_and_circuit_breaker():
    story = Story(
        title="Flood warnings issued after heavy rain",
        source_name="Reuters",
        source_url="https://reuters.com/floods",
        published_at=datetime.now(timezone.utc),
        raw_summary="Officials issued flood warnings after heavy rain affected several districts.",
    )
    client = Mock()
    client.chat.completions.create.side_effect = RuntimeError("CUDA out of memory")
    reset_generation_backend_state()
    try:
        with patch("pipeline.generator._get_client", return_value=client):
            first = generate_post(story)
            second = generate_post(
                Story(
                    title="Emergency teams respond to regional flooding",
                    source_name="Associated Press",
                    source_url="https://apnews.com/floods",
                    published_at=datetime.now(timezone.utc),
                    raw_summary="Emergency teams responded as flooding disrupted roads across the region.",
                )
            )
        assert first.post_content
        assert second.post_content
        assert client.chat.completions.create.call_count == 1
    finally:
        reset_generation_backend_state()
