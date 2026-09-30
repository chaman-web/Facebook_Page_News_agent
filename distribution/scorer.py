"""Distribution score, evaluated only after hard matching gates pass."""

from __future__ import annotations

from datetime import datetime, timezone

from distribution.models import DistributionStory, GeoResult, GroupRecord, TopicResult


def score(story: DistributionStory, geo: GeoResult, topic: TopicResult, group: GroupRecord, *, now: datetime | None = None) -> dict[str, int]:
    countries = {geo.primary_country, *geo.additional_countries}
    geographic = 40 if countries & set(group.countries) else 0
    story_topics = {topic.primary_topic, *topic.secondary_topics}
    topic_score = 30 if story_topics & set(group.allowed_topics) else 0
    importance = max(0, min(15, round(story.importance_score * 0.15)))
    current = now or datetime.now(timezone.utc)
    published = datetime.fromisoformat(story.publication_time)
    if published.tzinfo is None: published = published.replace(tzinfo=timezone.utc)
    age_hours = max(0, (current - published).total_seconds() / 3600)
    freshness = 10 if age_hours <= 24 else 7 if age_hours <= 48 else 3 if age_hours <= 96 else 0
    suitability = 5 if group.allow_news_posts and group.allow_links and group.allow_page_content else 0
    return {"geographic_score": geographic, "topic_score": topic_score, "importance_score": importance, "freshness_score": freshness, "suitability_score": suitability, "distribution_score": geographic + topic_score + importance + freshness + suitability}
