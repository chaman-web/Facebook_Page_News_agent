"""Configuration isolated from the production publishing pipeline."""

from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


GROUP_RECOMMENDER_ENABLED = _bool("GROUP_RECOMMENDER_ENABLED", False)
GROUP_AUTO_SHARE_ENABLED = False
AUTOMATED_GROUP_POSTING_SUPPORTED = False
OFFICIAL_META_API_VERIFIED = False
GROUP_RULE_REVIEW_DAYS = int(os.getenv("GROUP_RULE_REVIEW_DAYS", "30"))
MAX_GROUP_RECOMMENDATIONS_PER_STORY = min(3, max(1, int(os.getenv("MAX_GROUP_RECOMMENDATIONS_PER_STORY", "3"))))
MAX_DAILY_DISTRIBUTED_POSTS = min(3, max(2, int(os.getenv("MAX_DAILY_DISTRIBUTED_POSTS", "3"))))
MIN_GROUP_REPORTED_MEMBERS = max(100000, int(os.getenv("MIN_GROUP_REPORTED_MEMBERS", "100000")))
RECOMMEND_SCORE = int(os.getenv("GROUP_RECOMMEND_SCORE", "80"))
REVIEW_SCORE = int(os.getenv("GROUP_REVIEW_SCORE", "60"))
