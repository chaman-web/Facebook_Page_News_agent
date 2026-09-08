"""Tests for evidence-based story verification."""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault("NEWSAPI_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from models import Story, StoryRejected, VerificationStatus  # noqa: E402
from news.article_extractor import ArticleContent  # noqa: E402
from pipeline.verifier import verify_story  # noqa: E402


def _story(
    title: str = "100 people killed after major city attack",
    summary: str = "Officials said 100 people were killed after an attack in the city.",
    url: str = "https://reuters.com/story",
) -> Story:
    return Story(
        title=title,
        source_name="Reuters",
        source_url=url,
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
        source_tier=1,
        confidence="GOOD",
    )


def _source(
    name: str = "BBC News",
    url: str = "https://bbc.com/news/story",
    title: str = "City attack leaves 100 people dead",
    summary: str = "Authorities confirmed that 100 people were killed in the city attack.",
    tier: int = 1,
) -> dict:
    return {
        "name": name,
        "url": url,
        "domain": url.split("/")[2],
        "title": title,
        "summary": summary,
        "tier": tier,
    }


class TestVerifyStory:
    def test_two_independent_matching_sources_are_verified(self):
        story = _story()
        story.corroborating_sources = [_source()]

        result = verify_story(story)

        assert result.verification_status == VerificationStatus.VERIFIED
        assert result.verification_score >= 80
        assert len(result.verification_evidence) == 2

    def test_single_strong_source_is_provisional(self):
        result = verify_story(_story())

        assert result.verification_status == VerificationStatus.UNVERIFIED
        assert "do not auto-publish" in result.verification_reason

    def test_same_domain_does_not_count_as_independent(self):
        story = _story()
        story.corroborating_sources = [
            _source(name="Reuters World", url="https://feeds.reuters.com/other-story")
        ]

        result = verify_story(story)

        assert result.verification_status == VerificationStatus.UNVERIFIED

    def test_related_publisher_domains_do_not_count_twice(self):
        story = _story(url="https://bbc.com/news/story")
        story.source_name = "BBC News"
        story.corroborating_sources = [
            _source(name="BBC News UK", url="https://bbc.co.uk/news/another-story")
        ]
        result = verify_story(story)
        assert result.verification_status == VerificationStatus.UNVERIFIED

    def test_syndicated_copy_does_not_count_as_independent(self):
        repeated = "The same wire report describes the city attack and confirmed casualties. " * 5
        story = _story(summary=repeated)
        story.corroborating_sources = [_source(summary=repeated)]

        result = verify_story(story)

        assert result.verification_status == VerificationStatus.UNVERIFIED
        assert result.verification_evidence[1]["syndicated_copy"] is True

    def test_material_count_conflict_is_rejected(self):
        story = _story()
        story.corroborating_sources = [
            _source(
                title="City attack leaves 120 people dead",
                summary="Authorities said 120 people were killed in the city attack.",
            )
        ]

        with pytest.raises(StoryRejected, match="conflict"):
            verify_story(story)

        assert story.verification_status == VerificationStatus.SPECULATIVE

    def test_deep_verification_uses_public_article_text(self):
        story = _story(summary="Brief report.")
        story.corroborating_sources = [_source(summary="Brief report of the same attack.")]
        pages = [
            ArticleContent(text="Officials confirmed that 100 people were killed in the city attack."),
            ArticleContent(text="Authorities confirmed 100 people were killed in the same city attack."),
        ]

        with patch("pipeline.verifier.fetch_article", side_effect=pages):
            result = verify_story(story, deep=True)

        assert result.article_text.startswith("Officials confirmed")
        assert result.verification_status == VerificationStatus.VERIFIED

    def test_unreliable_domain_is_rejected(self):
        story = _story(url="https://infowars.com/story")
        story.source_tier = 5

        with pytest.raises(StoryRejected, match="Unreliable source"):
            verify_story(story)

        assert story.verification_status == VerificationStatus.SPECULATIVE
