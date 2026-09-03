"""
tests/test_verifier.py — Unit tests for story verification logic.
"""

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("NEWSAPI_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from models import Story, StoryRejected, VerificationStatus  # noqa: E402
from pipeline.verifier import verify_story  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _story(title: str = "World Leaders Meet for Climate Summit", url: str = "https://reuters.com/story") -> Story:
    return Story(
        title=title,
        source_name="Reuters",
        source_url=url,
        published_at=datetime.now(timezone.utc),
        raw_summary="Leaders from 50 countries gathered to discuss climate action.",
    )


def _mock_corroborating(n: int) -> list[dict]:
    return [{"name": f"Source {i}", "url": f"https://source{i}.com/story"} for i in range(n)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVerifyStory:

    def test_two_or_more_sources_sets_verified(self):
        story = _story()
        with patch("pipeline.verifier._search_corroborating", return_value=_mock_corroborating(2)):
            result = verify_story(story)
        assert result.verification_status == VerificationStatus.VERIFIED
        assert len(result.corroborating_sources) == 2

    def test_one_source_sets_unverified(self):
        story = _story()
        with patch("pipeline.verifier._search_corroborating", return_value=_mock_corroborating(1)):
            result = verify_story(story)
        assert result.verification_status == VerificationStatus.UNVERIFIED

    def test_zero_sources_sets_unverified(self):
        story = _story()
        with patch("pipeline.verifier._search_corroborating", return_value=[]):
            result = verify_story(story)
        assert result.verification_status == VerificationStatus.UNVERIFIED

    def test_unreliable_domain_raises_story_rejected(self):
        story = _story(url="https://infowars.com/story-123")
        with pytest.raises(StoryRejected, match="unreliable list"):
            verify_story(story)

    def test_unreliable_domain_sets_speculative(self):
        story = _story(url="https://naturalnews.com/story")
        with pytest.raises(StoryRejected):
            verify_story(story)
        assert story.verification_status == VerificationStatus.SPECULATIVE
