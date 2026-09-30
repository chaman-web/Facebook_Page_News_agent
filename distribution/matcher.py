"""Hard size/geography/topic matching before scoring."""

from distribution import settings
from distribution.models import GeoResult, GroupRecord, TopicResult


def match(geo: GeoResult, topic: TopicResult, group: GroupRecord) -> tuple[bool, str]:
    if group.reported_member_count is not None and group.reported_member_count < settings.MIN_GROUP_REPORTED_MEMBERS:
        return False, "GROUP_BELOW_MINIMUM_SIZE"
    countries = {geo.primary_country, *geo.additional_countries}
    if not countries & set(group.countries) and "GLOBAL" not in group.countries:
        return False, "GROUP_SCOPE_MISMATCH"
    topics = {topic.primary_topic, *topic.secondary_topics}
    if not topics & set(group.allowed_topics):
        return False, "GROUP_TOPIC_MISMATCH"
    return True, "PASS"
