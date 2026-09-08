"""
models.py — Core data structures and exceptions for the Facebook News Agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    SPECULATIVE = "SPECULATIVE"


class DraftStatus(str, Enum):
    DRAFT = "DRAFT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------------------
# Story dataclass
# ---------------------------------------------------------------------------

@dataclass
class Story:
    # --- Discovery ---
    title: str
    source_name: str
    source_url: str
    published_at: datetime
    raw_summary: str

    # --- Source quality (assigned at fetch time) ---
    source_tier: int = 4             # SourceTier int (1=wire, 2=established, 3=specialist, 4=blog, 5=unreliable)
    confidence: str = "LOW"          # HIGH / GOOD / LOW / REJECT (from clusterer)
    cluster_size: int = 1            # Number of articles reporting the same event

    # --- Verification ---
    corroborating_sources: list[dict] = field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_score: float = 0.0
    verification_reason: str = ""
    verification_evidence: list[dict] = field(default_factory=list)
    article_text: str = ""
    article_image_url: str | None = None
    region: str = "global"
    priority_protected: bool = False

    # --- Generation ---
    post_content: str | None = None
    card_headline: str | None = None   # short punchy headline for the image card
    card_description: str | None = None  # distinct, grounded detail shown above the logo
    hashtags: list[str] = field(default_factory=list)
    category: str = "breaking"  # news category slug

    # --- Draft ---
    draft_status: DraftStatus = DraftStatus.DRAFT
    rejection_reason: str | None = None
    generated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class NewsSourceError(Exception):
    """Raised when news cannot be fetched from any source."""


class StoryRejected(Exception):
    """Raised when a story fails verification or quality checks."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DuplicateStory(Exception):
    """Raised when the story has already been processed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GenerationError(Exception):
    """Raised when the LLM fails to generate post content."""
