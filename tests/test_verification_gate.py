"""Integration checks for separation of importance and publish eligibility."""

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("NEWSAPI_KEY", "test-key")

from facebook.publisher import FacebookPublishError, publish_post  # noqa: E402
from agent import _select_verified_and_provisional  # noqa: E402
from models import DraftStatus, Story, VerificationStatus  # noqa: E402
from pipeline.editorial_scorer import score_story  # noqa: E402


def _powerful_provisional_story() -> Story:
    return Story(
        title="Breaking: Major earthquake kills 100 people after historic disaster",
        source_name="Reuters",
        source_url="https://reuters.com/major-earthquake",
        published_at=datetime.now(timezone.utc),
        raw_summary=(
            "Officials reported that a major earthquake killed 100 people and caused "
            "an emergency across multiple cities. Rescue teams are searching for survivors."
        ),
        source_tier=1,
        confidence="GOOD",
        verification_status=VerificationStatus.UNVERIFIED,
        verification_score=55,
        verification_reason="Awaiting an independent source.",
        post_content="A sufficiently long provisional Facebook post that must never publish automatically.",
        draft_status=DraftStatus.READY_FOR_REVIEW,
    )


def test_editorial_score_preserves_powerful_provisional_story():
    score = score_story(_powerful_provisional_story())

    # The previous implementation capped this at 65 solely because it was
    # unverified. Its editorial value must now remain independent of that gate.
    assert score.total > 65
    assert "separate gate" in score.reason


def test_publisher_rejects_provisional_story_before_network_call():
    with pytest.raises(FacebookPublishError, match="independent-source verification"):
        publish_post(_powerful_provisional_story())


def test_publisher_rejects_verified_story_without_image_card():
    story = _powerful_provisional_story()
    story.verification_status = VerificationStatus.VERIFIED
    story.verification_score = 90
    with pytest.raises(FacebookPublishError, match="image card is required"):
        publish_post(story)


def test_selection_keeps_verified_and_provisional_candidates_separately():
    provisional = _powerful_provisional_story()
    verified = _powerful_provisional_story()
    verified.title = "Verified major developing story"
    verified.source_url = "https://bbc.com/verified-story"
    verified.verification_status = VerificationStatus.VERIFIED
    verified.verification_score = 85

    selected = _select_verified_and_provisional(
        [(score_story(provisional), provisional), (score_story(verified), verified)],
        per_lane=1,
    )

    assert [story.verification_status for _, story in selected] == [
        VerificationStatus.VERIFIED,
        VerificationStatus.UNVERIFIED,
    ]
