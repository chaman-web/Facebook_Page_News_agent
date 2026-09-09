"""Integration checks for separation of importance and publish eligibility."""

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault("NEWSAPI_KEY", "test-key")

from facebook.publisher import FacebookPublishError, publish_post, publish_post_with_image  # noqa: E402
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


def test_selection_keeps_all_verified_tier_candidates_each_fetch_run():
    verified_stories = []
    for index in range(3):
        story = _powerful_provisional_story()
        story.title = f"Verified consequential story {index}"
        story.source_url = f"https://bbc.com/verified-{index}"
        story.verification_status = VerificationStatus.VERIFIED
        story.verification_score = 85
        verified_stories.append(story)

    selected = _select_verified_and_provisional(
        [(score_story(story), story) for story in verified_stories],
        per_lane=1,
    )

    assert [story.source_url for _, story in selected] == [
        story.source_url for story in verified_stories
    ]


def test_synthetic_image_disclosure_is_added_to_facebook_caption(tmp_path):
    story = Story(
        title="Technology company releases a new processor",
        source_name="Reuters",
        source_url="https://reuters.com/technology",
        published_at=datetime.now(timezone.utc),
        raw_summary="The company released a new processor for consumer computers.",
        verification_status=VerificationStatus.VERIFIED,
        verification_score=90,
        post_content="💻 A technology company released a new processor.\nThe product is intended for consumer computers.",
        draft_status=DraftStatus.READY_FOR_REVIEW,
        image_is_synthetic=True,
        image_provenance="pollinations_ai",
        image_credit="Pollinations.ai",
    )
    image_path = tmp_path / "card.jpg"
    image_path.write_bytes(b"image")

    with (
        patch("facebook.publisher.config.FACEBOOK_PAGE_ID", "page-1"),
        patch("facebook.publisher.config.FACEBOOK_PAGE_TOKEN", "token"),
        patch("facebook.publisher.TokenManager.get_valid_token", return_value="token"),
        patch("facebook.publisher._publish_with_photo", return_value="post-1") as publish,
    ):
        publish_post_with_image(story, image_path)

    assert "AI-generated illustration" in publish.call_args.args[0]


def test_publisher_stops_sensitive_synthetic_news_image(tmp_path):
    story = _powerful_provisional_story()
    story.verification_status = VerificationStatus.VERIFIED
    story.verification_score = 90
    story.image_is_synthetic = True
    story.image_provenance = "pollinations_ai"
    image_path = tmp_path / "earthquake.jpg"
    image_path.write_bytes(b"image")

    with pytest.raises(FacebookPublishError, match="Meta policy review required"):
        publish_post_with_image(story, image_path)
