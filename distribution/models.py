"""Typed records for the isolated distribution recommender."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Decision(str, Enum):
    RECOMMEND = "RECOMMEND"
    REVIEW = "REVIEW"
    SKIP = "SKIP"
    BLOCK = "BLOCK"
    WAIT = "WAIT"


class PolicyVerdict(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class DistributionStory:
    story_id: str
    facebook_post_id: str
    facebook_post_url: str
    facebook_publish_confirmed: bool
    headline: str
    verified_claims: list[str]
    evidence: list[dict]
    category: str
    sources: list[str]
    publication_time: str
    event_time: str | None
    verified_locations: list[str]
    verified_people: list[str]
    verified_organizations: list[str]
    verification_status: str
    importance_score: float = 0.0


@dataclass(frozen=True)
class GeoResult:
    primary_country: str | None
    primary_region: str | None
    additional_countries: list[str]
    geographic_scope: str
    confidence: float
    evidence: list[str]


@dataclass(frozen=True)
class TopicResult:
    primary_topic: str | None
    secondary_topics: list[str]
    confidence: float
    evidence: list[str]


@dataclass(frozen=True)
class GroupRecord:
    group_id: str
    group_name: str
    group_url: str
    countries: list[str]
    regions: list[str]
    allowed_topics: list[str]
    blocked_topics: list[str]
    active: bool
    approved: bool
    public_or_private: str
    allow_news_posts: bool
    allow_links: bool
    allow_page_content: bool
    manual_approval_required: bool
    cooldown_minutes: int
    daily_recommendation_limit: int
    rules_url: str
    rules_summary: str
    rules_checked_at: str
    approved_by: str
    approved_at: str
    notes: str = ""
    reported_member_count: int | None = None


@dataclass(frozen=True)
class CapabilityStatus:
    automated_group_posting_supported: bool = False
    authorization_valid: bool = False
    required_permissions_available: bool = False
    destination_supported: bool = False
    official_api_verified: bool = False
    checked_at: str = ""


@dataclass
class Recommendation:
    story_id: str
    facebook_post_id: str
    facebook_post_url: str
    primary_country: str | None
    primary_region: str | None
    additional_countries: list[str]
    geographic_scope: str
    geo_confidence: float
    primary_topic: str | None
    secondary_topics: list[str]
    topic_confidence: float
    group_id: str
    group_name: str
    group_url: str
    geographic_score: int
    topic_score: int
    importance_score: int
    freshness_score: int
    suitability_score: int
    distribution_score: int
    policy_verdict: str
    policy_reasons: list[str] = field(default_factory=list)
    decision: str = Decision.SKIP.value
    auto_share: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
