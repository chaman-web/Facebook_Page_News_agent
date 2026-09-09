"""
tests/test_draft_writer.py — Unit tests for draft file output.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("NEWSAPI_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from models import DraftStatus, Story, VerificationStatus  # noqa: E402
from output.draft_writer import save_draft  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _story(
    verification: VerificationStatus = VerificationStatus.VERIFIED,
    post_content: str = "This is the generated Facebook post.",
) -> Story:
    return Story(
        title="Global Leaders Agree on New Trade Deal",
        source_name="BBC News",
        source_url="https://bbc.com/news/trade-deal",
        published_at=datetime.now(timezone.utc),
        raw_summary="World leaders have reached a landmark trade agreement.",
        verification_status=verification,
        corroborating_sources=[{"name": "Reuters", "url": "https://reuters.com/trade"}],
        post_content=post_content,
        hashtags=["#WorldNews", "#Trade", "#GlobalEconomy"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSaveDraft:

    def test_json_file_contains_required_fields(self, tmp_path):
        story = _story()
        with patch("config.DRAFTS_DIR", str(tmp_path)):
            path = save_draft(story)

        data = json.loads(Path(path).read_text())

        required_fields = [
            "title", "source_name", "source_url", "published_at",
            "news_summary", "verification_status", "post_content",
            "hashtags", "draft_status", "rejection_reason", "generated_at",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    def test_markdown_file_is_created(self, tmp_path):
        story = _story()
        with patch("config.DRAFTS_DIR", str(tmp_path)):
            json_path = save_draft(story)

        md_path = Path(json_path).with_suffix(".md")
        assert md_path.exists(), "Markdown file was not created"
        assert len(md_path.read_text()) > 0

    def test_verified_story_gets_ready_for_review(self, tmp_path):
        story = _story(verification=VerificationStatus.VERIFIED)
        with patch("config.DRAFTS_DIR", str(tmp_path)):
            path = save_draft(story)

        data = json.loads(Path(path).read_text())
        assert data["draft_status"] == DraftStatus.READY_FOR_REVIEW.value

    def test_unverified_story_gets_draft_status(self, tmp_path):
        story = _story(verification=VerificationStatus.UNVERIFIED)
        with patch("config.DRAFTS_DIR", str(tmp_path)):
            path = save_draft(story)

        data = json.loads(Path(path).read_text())
        assert data["draft_status"] == DraftStatus.DRAFT.value

    def test_rejected_story_keeps_rejected_status(self, tmp_path):
        story = _story()
        story.draft_status = DraftStatus.REJECTED
        story.rejection_reason = "Source is unreliable."
        with patch("config.DRAFTS_DIR", str(tmp_path)):
            path = save_draft(story)

        data = json.loads(Path(path).read_text())
        assert data["draft_status"] == DraftStatus.REJECTED.value
        assert data["rejection_reason"] == "Source is unreliable."

    def test_policy_review_story_keeps_protected_status(self, tmp_path):
        story = _story()
        story.draft_status = DraftStatus.POLICY_REVIEW
        story.policy_decision = "REVIEW"
        story.policy_categories = ["graphic_violence"]
        story.policy_reasons = ["Potentially graphic description."]
        story.policy_version = "2026-09"
        with patch("config.DRAFTS_DIR", str(tmp_path)):
            path = save_draft(story)

        data = json.loads(Path(path).read_text())
        assert data["draft_status"] == DraftStatus.POLICY_REVIEW.value
        assert data["policy_categories"] == ["graphic_violence"]
