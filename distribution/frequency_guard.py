"""Daily-volume, destination cooldown and duplicate guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from distribution import settings
from distribution.models import GroupRecord


def check(facebook_post_id: str, group: GroupRecord, records: list[dict], *, now: datetime | None = None) -> tuple[bool, str]:
    current = now or datetime.now(timezone.utc)
    if any(str(item.get("facebook_post_id")) == facebook_post_id and str(item.get("group_id")) == group.group_id for item in records):
        return False, "DUPLICATE_DISTRIBUTION"
    today = current.date()
    todays = [item for item in records if _time(item).date() == today]
    distinct_posts = {str(item.get("facebook_post_id")) for item in todays}
    if len(distinct_posts) >= settings.MAX_DAILY_DISTRIBUTED_POSTS:
        return False, "DAILY_POST_LIMIT_REACHED"
    if any(str(item.get("group_id")) == group.group_id for item in todays):
        return False, "DESTINATION_ALREADY_USED_TODAY"
    group_today = sum(1 for item in todays if str(item.get("group_id")) == group.group_id)
    if group_today >= group.daily_recommendation_limit:
        return False, "GROUP_DAILY_LIMIT_REACHED"
    prior = [_time(item) for item in records if str(item.get("group_id")) == group.group_id]
    if prior and current - max(prior) < timedelta(minutes=group.cooldown_minutes):
        return False, "GROUP_COOLDOWN_ACTIVE"
    return True, "PASS"


def _time(item: dict) -> datetime:
    try: value = datetime.fromisoformat(str(item.get("recommended_at", "")))
    except ValueError: return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
