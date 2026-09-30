"""Hard policy and Group-rule gates; scores cannot override these results."""

from __future__ import annotations

from datetime import date, datetime, timezone

from distribution import settings
from distribution.models import DistributionStory, GeoResult, GroupRecord, PolicyVerdict, TopicResult


def evaluate(story: DistributionStory, geo: GeoResult, topic: TopicResult, group: GroupRecord, *, today: date | None = None) -> tuple[PolicyVerdict, list[str]]:
    reasons: list[str] = []
    if story.verification_status != "VERIFIED": reasons.append("UNDERLYING_POST_NOT_VERIFIED")
    if not group.active: reasons.append("GROUP_INACTIVE")
    if not group.approved: reasons.append("GROUP_NOT_APPROVED")
    if not group.allow_links: reasons.append("GROUP_LINKS_NOT_ALLOWED")
    if not group.allow_news_posts or not group.allow_page_content: reasons.append("GROUP_CONTENT_NOT_ALLOWED")
    if topic.primary_topic in group.blocked_topics: reasons.append("GROUP_TOPIC_BLOCKED")
    if reasons:
        return PolicyVerdict.BLOCK, reasons

    review: list[str] = []
    if geo.geographic_scope == "UNKNOWN" or geo.confidence < 0.60: review.append("GEO_CLASSIFICATION_UNCERTAIN")
    if topic.primary_topic is None or topic.confidence < 0.60: review.append("TOPIC_CLASSIFICATION_UNCERTAIN")
    if group.reported_member_count is None: review.append("GROUP_SIZE_REVIEW_REQUIRED")
    try:
        checked = datetime.fromisoformat(group.rules_checked_at).date()
        if ((today or datetime.now(timezone.utc).date()) - checked).days > settings.GROUP_RULE_REVIEW_DAYS:
            review.append("GROUP_RULES_REVIEW_REQUIRED")
    except ValueError:
        review.append("GROUP_RULES_REVIEW_REQUIRED")
    if review:
        return PolicyVerdict.REVIEW, review
    return PolicyVerdict.PASS, []
