"""Plan two or three daily recommendations across distinct approved Groups."""

from __future__ import annotations

from datetime import datetime

from distribution import settings
from distribution.models import Decision, DistributionStory, Recommendation
from distribution.recommender import DistributionRecommender


def plan_daily(
    recommender: DistributionRecommender,
    stories: list[DistributionStory],
    *,
    preview: bool = True,
    now: datetime | None = None,
) -> list[Recommendation]:
    """Choose up to the configured daily cap, never repeating a destination."""
    selected: list[Recommendation] = []
    used_groups: set[str] = set()
    ranked = sorted(stories, key=lambda item: (-item.importance_score, item.publication_time))
    for story in ranked:
        candidates = recommender.recommend(story, preview=preview, now=now)
        choice = next(
            (
                item for item in candidates
                if item.decision == Decision.RECOMMEND.value and item.group_id not in used_groups
            ),
            None,
        )
        if choice is None:
            continue
        selected.append(choice)
        used_groups.add(choice.group_id)
        if len(selected) >= settings.MAX_DAILY_DISTRIBUTED_POSTS:
            break
    return selected
