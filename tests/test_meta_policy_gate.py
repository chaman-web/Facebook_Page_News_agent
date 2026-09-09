from datetime import datetime, timezone

from models import Story
from pipeline.meta_policy_gate import PolicyDecision, evaluate_meta_policy


def _story(title: str, summary: str, post: str | None = None, **kwargs) -> Story:
    return Story(
        title=title,
        source_name="Reuters",
        source_url="https://reuters.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
        post_content=post or summary,
        image_provenance=kwargs.pop("image_provenance", "branded_fallback"),
        **kwargs,
    )


def test_normal_war_reporting_is_not_blocked_by_sensitive_keywords(tmp_path):
    story = _story(
        "Government reports new developments in the war",
        "Officials reported new diplomatic talks while fighting continued near the border.",
    )
    assert evaluate_meta_policy(story, tmp_path / "card.jpg").decision == PolicyDecision.PASS


def test_direct_violence_incitement_is_blocked():
    story = _story("A direct call", "Everyone should attack them immediately.")
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.BLOCK
    assert "violence_incitement" in verdict.categories


def test_reported_incitement_is_preserved_for_review():
    story = _story(
        "Police investigate threatening statement",
        "Police reported that a speaker said everyone should attack them immediately.",
    )
    assert evaluate_meta_policy(story).decision == PolicyDecision.REVIEW


def test_graphic_description_requires_review():
    story = _story("Investigation continues", "Police discovered a severed limb at the scene.")
    assert evaluate_meta_policy(story).decision == PolicyDecision.REVIEW


def test_private_identifier_requires_review():
    story = _story("Personal data exposed", "The victim's passport number is 123456789.")
    assert evaluate_meta_policy(story).decision == PolicyDecision.REVIEW


def test_engagement_bait_requires_review():
    story = _story("Community update", "Share this post if you agree with the decision.")
    assert evaluate_meta_policy(story).decision == PolicyDecision.REVIEW


def test_unknown_image_rights_require_review(tmp_path):
    story = _story("Economic update", "The central bank announced an interest-rate decision.")
    story.image_provenance = ""
    verdict = evaluate_meta_policy(story, tmp_path / "legacy.jpg")
    assert verdict.decision == PolicyDecision.REVIEW
    assert "image_rights" in verdict.categories


def test_sensitive_synthetic_image_requires_review(tmp_path):
    story = _story(
        "Earthquake leaves dozens dead",
        "Officials reported deaths after a major earthquake.",
        image_provenance="pollinations_ai",
        image_is_synthetic=True,
    )
    verdict = evaluate_meta_policy(story, tmp_path / "synthetic.jpg")
    assert verdict.decision == PolicyDecision.REVIEW
    assert "synthetic_sensitive_event" in verdict.categories
