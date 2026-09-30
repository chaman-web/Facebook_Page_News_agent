"""Orchestrate manual-only recommendations without production-pipeline hooks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from distribution import settings
from distribution.frequency_guard import check as frequency_check
from distribution.geo_classifier import classify_geography
from distribution.group_registry import load_groups
from distribution.ledger import append, load_latest
from distribution.matcher import match
from distribution.models import Decision, DistributionStory, PolicyVerdict, Recommendation
from distribution.policy_gate import evaluate
from distribution.scorer import score
from distribution.topic_classifier import classify_topic


class DistributionRecommender:
    def __init__(self, registry_path: Path, ledger_path: Path) -> None:
        self.registry_path = registry_path
        self.ledger_path = ledger_path

    def recommend(self, story: DistributionStory, *, preview: bool = False, now: datetime | None = None) -> list[Recommendation]:
        if not preview and not settings.GROUP_RECOMMENDER_ENABLED:
            raise RuntimeError("GROUP_RECOMMENDER_ENABLED is false; use preview mode or enable recommendations explicitly.")
        if settings.GROUP_AUTO_SHARE_ENABLED:
            raise RuntimeError("Phase 1 refuses GROUP_AUTO_SHARE_ENABLED=true.")
        if not story.facebook_publish_confirmed or not story.facebook_post_id:
            return []
        geo = classify_geography(story)
        topic = classify_topic(story)
        records = list(load_latest(self.ledger_path).values())
        results: list[Recommendation] = []
        for group in load_groups(self.registry_path):
            matched, mismatch = match(geo, topic, group)
            verdict, reasons = evaluate(story, geo, topic, group, today=(now or datetime.now(timezone.utc)).date())
            allowed, frequency_reason = frequency_check(story.facebook_post_id, group, records, now=now)
            values = score(story, geo, topic, group, now=now)
            decision = Decision.SKIP
            if verdict == PolicyVerdict.BLOCK: decision = Decision.BLOCK
            elif verdict == PolicyVerdict.REVIEW: decision = Decision.REVIEW
            elif not matched: reasons.append(mismatch)
            elif not allowed: decision = Decision.WAIT if "COOLDOWN" in frequency_reason or "LIMIT" in frequency_reason or "TODAY" in frequency_reason else Decision.SKIP; reasons.append(frequency_reason)
            elif values["distribution_score"] >= settings.RECOMMEND_SCORE: decision = Decision.RECOMMEND
            elif values["distribution_score"] >= settings.REVIEW_SCORE: decision = Decision.REVIEW
            recommendation = Recommendation(
                story.story_id, story.facebook_post_id, story.facebook_post_url,
                geo.primary_country, geo.primary_region, geo.additional_countries, geo.geographic_scope, geo.confidence,
                topic.primary_topic, topic.secondary_topics, topic.confidence,
                group.group_id, group.group_name, group.group_url,
                policy_verdict=verdict.value, policy_reasons=reasons, decision=decision.value,
                **values,
            )
            results.append(recommendation)
        results.sort(key=lambda item: (item.decision != Decision.RECOMMEND.value, -item.distribution_score, item.group_name))
        selected = results[: settings.MAX_GROUP_RECOMMENDATIONS_PER_STORY]
        if not preview:
            for item in selected:
                if item.decision in {Decision.RECOMMEND.value, Decision.REVIEW.value}:
                    append(self.ledger_path, {**item.to_dict(), "recommendation_status": item.decision})
        return selected
