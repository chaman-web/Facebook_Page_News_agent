"""Safety and matching tests for manual-only Group recommendations."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pytest

from distribution import settings
from distribution.daily_planner import plan_daily
from distribution.frequency_guard import check as frequency_check
from distribution.geo_classifier import classify_geography
from distribution.ledger import append, load_latest, record_manual_submission
from distribution.matcher import match
from distribution.models import DistributionStory, GroupRecord, PolicyVerdict
from distribution.policy_gate import evaluate
from distribution.recommender import DistributionRecommender
from distribution.topic_classifier import classify_topic


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def story(*, country="US", category="world", claims=None, locations=None, verified=True, post_id="post-1"):
    defaults = {
        "US": (["California, United States"], ["Officials in California announced a verified update."]),
        "PK": (["Lahore, Pakistan"], ["Employers in Lahore, Pakistan announced new jobs and hiring."]),
        "AE": (["Dubai, United Arab Emirates"], ["Businesses in Dubai reported a verified market update."]),
        "GLOBAL": (["GLOBAL"], ["A global technology standard was announced."]),
        "UNKNOWN": ([], ["Officials announced an update."]),
    }
    default_locations, default_claims = defaults[country]
    return DistributionStory(
        story_id=f"story-{post_id}", facebook_post_id=post_id,
        facebook_post_url=f"https://facebook.test/{post_id}", facebook_publish_confirmed=True,
        headline="Verified update",
        verified_claims=claims or default_claims, evidence=[{"source": "Reuters"}],
        category=category, sources=["Reuters", "AP"],
        publication_time=(NOW - timedelta(hours=3)).isoformat(), event_time=None,
        verified_locations=locations if locations is not None else default_locations,
        verified_people=[], verified_organizations=[],
        verification_status="VERIFIED" if verified else "UNVERIFIED", importance_score=90,
    )


def group(*, group_id="g1", countries=None, topics=None, **changes):
    values = dict(
        group_id=group_id, group_name=f"Group {group_id}", group_url=f"https://facebook.test/groups/{group_id}",
        countries=countries or ["US"], regions=[], allowed_topics=topics or ["world"], blocked_topics=[],
        active=True, approved=True, public_or_private="PUBLIC", allow_news_posts=True,
        allow_links=True, allow_page_content=True, manual_approval_required=True,
        cooldown_minutes=1440, daily_recommendation_limit=1,
        rules_url="https://facebook.test/rules", rules_summary="Links and Page news allowed.",
        rules_checked_at="2026-09-30", approved_by="owner", approved_at="2026-09-30",
        reported_member_count=150000,
    )
    values.update(changes)
    return GroupRecord(**values)


def registry(tmp_path, groups):
    path = tmp_path / "groups.json"
    path.write_text(json.dumps({"groups": [asdict(item) for item in groups]}), encoding="utf-8")
    return path


def recommend(tmp_path, item, groups, ledger_records=None):
    ledger = tmp_path / "ledger.jsonl"
    if ledger_records:
        ledger.write_text("".join(json.dumps(row) + "\n" for row in ledger_records), encoding="utf-8")
    return DistributionRecommender(registry(tmp_path, groups), ledger).recommend(item, preview=True, now=NOW)


def test_us_general_story_recommends_approved_us_news_group(tmp_path):
    result = recommend(tmp_path, story(), [group()])[0]
    assert result.decision == "RECOMMEND"
    assert result.primary_country == "US"
    assert result.auto_share is False


def test_us_tech_matches_tech_but_rejects_sports(tmp_path):
    item = story(category="technology", claims=["A technology company in California announced artificial intelligence software."])
    results = recommend(tmp_path, item, [group(group_id="tech", topics=["tech"]), group(group_id="sports", topics=["sports"])])
    by_id = {result.group_id: result for result in results}
    assert by_id["tech"].decision == "RECOMMEND"
    assert by_id["sports"].decision == "SKIP"
    assert "GROUP_TOPIC_MISMATCH" in by_id["sports"].policy_reasons


@pytest.mark.parametrize("country,category,topic", [("PK", "jobs", "jobs"), ("AE", "business", "business")])
def test_country_topic_pairs(country, category, topic, tmp_path):
    result = recommend(tmp_path, story(country=country, category=category), [group(countries=[country], topics=[topic])])[0]
    assert result.decision == "RECOMMEND"


def test_global_tech_matches_only_global_group(tmp_path):
    item = story(country="GLOBAL", category="technology")
    results = recommend(tmp_path, item, [group(group_id="global", countries=["GLOBAL"], topics=["tech"]), group(group_id="us", topics=["tech"])])
    by_id = {result.group_id: result for result in results}
    assert by_id["global"].decision == "RECOMMEND"
    assert by_id["us"].decision == "SKIP"


def test_multi_country_story_keeps_additional_country():
    item = story(claims=["The United States and China announced a trade agreement."], locations=["United States", "China"])
    geo = classify_geography(item)
    assert geo.geographic_scope == "MULTI_COUNTRY"
    assert {geo.primary_country, *geo.additional_countries} == {"US", "CN"}


def test_unknown_geo_or_topic_requires_review(tmp_path):
    geo_result = recommend(tmp_path, story(country="UNKNOWN"), [group()])[0]
    topic_result = recommend(tmp_path, story(category="unknown", claims=["Officials announced an update."]), [group()], [])[0]
    assert geo_result.decision == "REVIEW"
    assert topic_result.decision == "REVIEW"


@pytest.mark.parametrize("changes,reason", [
    ({"approved": False}, "GROUP_NOT_APPROVED"), ({"active": False}, "GROUP_INACTIVE"),
    ({"allow_links": False}, "GROUP_LINKS_NOT_ALLOWED"), ({"allow_page_content": False}, "GROUP_CONTENT_NOT_ALLOWED"),
])
def test_hard_group_gates_block(changes, reason, tmp_path):
    result = recommend(tmp_path, story(), [group(**changes)])[0]
    assert result.decision == "BLOCK"
    assert reason in result.policy_reasons


def test_stale_rules_require_review(tmp_path):
    result = recommend(tmp_path, story(), [group(rules_checked_at="2026-01-01")])[0]
    assert result.decision == "REVIEW"
    assert "GROUP_RULES_REVIEW_REQUIRED" in result.policy_reasons


def test_unknown_group_size_requires_review(tmp_path):
    result = recommend(tmp_path, story(), [group(reported_member_count=None)])[0]
    assert result.decision == "REVIEW"
    assert "GROUP_SIZE_REVIEW_REQUIRED" in result.policy_reasons


def test_group_below_one_lac_is_skipped(tmp_path):
    result = recommend(tmp_path, story(), [group(reported_member_count=99999)])[0]
    assert result.decision == "SKIP"
    assert "GROUP_BELOW_MINIMUM_SIZE" in result.policy_reasons


def test_geographic_mismatch_skips(tmp_path):
    result = recommend(tmp_path, story(country="PK"), [group(countries=["US"])])[0]
    assert result.decision == "SKIP"
    assert "GROUP_SCOPE_MISMATCH" in result.policy_reasons


def test_duplicate_and_cooldown_are_prevented():
    g = group()
    duplicate = [{"facebook_post_id": "post-1", "group_id": "g1", "recommended_at": NOW.isoformat()}]
    assert frequency_check("post-1", g, duplicate, now=NOW)[1] == "DUPLICATE_DISTRIBUTION"
    cooldown = [{"facebook_post_id": "other", "group_id": "g1", "recommended_at": (NOW - timedelta(minutes=10)).isoformat()}]
    assert frequency_check("post-1", g, cooldown, now=NOW)[1] == "DESTINATION_ALREADY_USED_TODAY"


def test_daily_limit_is_three_posts_in_different_groups(tmp_path):
    records = [{"facebook_post_id": f"old-{i}", "group_id": f"old-group-{i}", "recommended_at": NOW.isoformat()} for i in range(3)]
    result = recommend(tmp_path, story(post_id="new"), [group(group_id="new-group")], records)[0]
    assert result.decision == "WAIT"
    assert "DAILY_POST_LIMIT_REACHED" in result.policy_reasons


def test_more_than_three_groups_returns_maximum_three(tmp_path):
    results = recommend(tmp_path, story(), [group(group_id=f"g{i}") for i in range(5)])
    assert len(results) == 3


def test_policy_block_overrides_high_score(tmp_path):
    result = recommend(tmp_path, replace(story(), importance_score=100), [group(allow_links=False)])[0]
    assert result.distribution_score >= 95
    assert result.policy_verdict == PolicyVerdict.BLOCK.value
    assert result.decision == "BLOCK"


def test_unverified_story_is_blocked(tmp_path):
    result = recommend(tmp_path, story(verified=False), [group()])[0]
    assert result.decision == "BLOCK"


def test_facebook_failure_does_not_run_recommender(tmp_path):
    item = replace(story(), facebook_publish_confirmed=False, facebook_post_id="")
    assert recommend(tmp_path, item, [group()]) == []


def test_manual_pending_submission_is_recorded_without_posting(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append(path, {"facebook_post_id": "post-1", "group_id": "g1", "decision": "RECOMMEND"})
    saved = record_manual_submission(path, "post-1", "g1", pending_admin=True)
    assert saved["delivery_status"] == "SUBMITTED_PENDING_ADMIN"
    assert load_latest(path)[("post-1", "g1")]["delivery_status"] == "SUBMITTED_PENDING_ADMIN"


def test_auto_share_is_permanently_disabled():
    assert settings.GROUP_AUTO_SHARE_ENABLED is False
    assert settings.AUTOMATED_GROUP_POSTING_SUPPORTED is False
    assert settings.OFFICIAL_META_API_VERIFIED is False


def test_non_preview_requires_explicit_recommender_enablement(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GROUP_RECOMMENDER_ENABLED", False)
    service = DistributionRecommender(registry(tmp_path, [group()]), tmp_path / "ledger.jsonl")
    with pytest.raises(RuntimeError, match="GROUP_RECOMMENDER_ENABLED is false"):
        service.recommend(story(), preview=False, now=NOW)


def test_recommender_failure_cannot_touch_publishers(tmp_path):
    service = DistributionRecommender(tmp_path / "missing.json", tmp_path / "ledger.jsonl")
    with pytest.raises(FileNotFoundError):
        service.recommend(story(), preview=True, now=NOW)
    assert not (tmp_path / "ledger.jsonl").exists()


def test_daily_planner_selects_three_posts_for_three_different_groups(tmp_path):
    groups = [group(group_id=f"g{i}") for i in range(3)]
    service = DistributionRecommender(registry(tmp_path, groups), tmp_path / "ledger.jsonl")
    items = [replace(story(post_id=f"post-{i}"), importance_score=100 - i) for i in range(5)]

    planned = plan_daily(service, items, preview=True, now=NOW)

    assert len(planned) == 3
    assert len({item.facebook_post_id for item in planned}) == 3
    assert len({item.group_id for item in planned}) == 3


def test_daily_planner_does_not_repeat_only_available_group(tmp_path):
    service = DistributionRecommender(registry(tmp_path, [group()]), tmp_path / "ledger.jsonl")
    items = [story(post_id=f"post-{i}") for i in range(3)]
    planned = plan_daily(service, items, preview=True, now=NOW)
    assert len(planned) == 1
