from datetime import datetime, timezone

from models import Story
from pipeline.generator import _grounded_fallback_post
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
    assert check_generated_facts(story, post).passed
