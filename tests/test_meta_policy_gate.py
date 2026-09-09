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


def test_unrelated_reporting_context_does_not_soften_direct_incitement():
    story = _story(
        "A direct call",
        "Everyone should attack them immediately. Police later issued an unrelated statement.",
    )
    assert evaluate_meta_policy(story).decision == PolicyDecision.BLOCK


def test_imperative_violence_against_group_is_blocked():
    story = _story("A direct call", "Kill all Muslims immediately.")
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.BLOCK
    assert "violence_incitement" in verdict.categories


def test_attributed_imperative_violence_is_preserved_for_review():
    story = _story(
        "Police investigate speech",
        "Police said the speaker shouted: kill all Muslims immediately.",
    )
    assert evaluate_meta_policy(story).decision == PolicyDecision.REVIEW


def test_self_harm_promotion_is_blocked():
    story = _story("Harmful advice", "Suicide is the answer.")
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.BLOCK
    assert "self_harm_instruction" in verdict.categories


def test_reported_regulated_goods_transaction_is_preserved_for_review():
    story = _story(
        "Arrest follows weapons investigation",
        "Police reported that the suspect tried to sell firearms online.",
    )
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.REVIEW
    assert "regulated_goods" in verdict.categories


def test_direct_regulated_goods_transaction_is_blocked():
    story = _story("Private offer", "Buy firearms and ammunition from us today.")
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.BLOCK
    assert "regulated_goods" in verdict.categories


def test_credential_theft_request_is_blocked():
    story = _story("Account confirmation", "Enter your password and verification code here.")
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.BLOCK
    assert "credential_theft" in verdict.categories


def test_human_exploitation_solicitation_is_blocked():
    story = _story("Recruitment offer", "Recruit workers for forced labor overseas.")
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.BLOCK
    assert "human_exploitation" in verdict.categories


def test_reported_sexual_exploitation_is_preserved_for_review():
    story = _story(
        "Police announce arrest",
        "Police said a suspect tried to share non-consensual sexual content.",
    )
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.REVIEW
    assert "sexual_exploitation" in verdict.categories


def test_self_harm_news_coverage_requires_review():
    story = _story(
        "Health service publishes suicide prevention report",
        "Officials reported new national suicide prevention measures.",
    )
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.REVIEW
    assert "self_harm_content" in verdict.categories


def test_normal_financial_reporting_remains_publishable():
    story = _story(
        "Central bank holds interest rates",
        "Officials said inflation slowed while the central bank held its policy rate.",
    )
    assert evaluate_meta_policy(story).decision == PolicyDecision.PASS


def test_policy_gate_checks_text_rendered_on_image_card():
    story = _story("Routine update", "Officials released a routine update.")
    story.card_headline = "Buy firearms and ammunition from us today"
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.BLOCK
    assert "regulated_goods" in verdict.categories


def test_policy_gate_checks_hashtags_added_by_publisher():
    story = _story("Routine update", "Officials released a routine update.")
    story.hashtags = ["#share_this_post_if_you_agree"]
    verdict = evaluate_meta_policy(story)
    assert verdict.decision == PolicyDecision.REVIEW
    assert "engagement_bait" in verdict.categories


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


def test_legacy_unknown_image_rights_require_review(tmp_path):
    story = _story("Economic update", "The central bank announced an interest-rate decision.")
    story.image_provenance = "legacy_unknown"
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
