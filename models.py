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

    # --- Verification ---
    corroborating_sources: list[dict] = field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED

    # --- Generation ---
    post_content: str | None = None
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
