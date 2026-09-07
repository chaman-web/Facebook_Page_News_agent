"""
pipeline/verifier.py — Story verification using cluster confidence.

After clustering, each story already has:
  - story.confidence    (HIGH / GOOD / LOW / REJECT from clusterer)
  - story.source_tier   (1–5)
  - story.corroborating_sources (other cluster members)

This module translates that cluster confidence into VerificationStatus
and applies the final gate: REJECT stories with no reliable source.

Confidence → VerificationStatus mapping:
  HIGH   → VERIFIED    (full editorial score)
  GOOD   → VERIFIED    (full editorial score)
  LOW    → UNVERIFIED  (score capped at 55, HOLD filler only)
  REJECT → StoryRejected (never published)
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from models import Story, StoryRejected, VerificationStatus
from pipeline.source_classifier import Confidence, SourceTier, TIER5_DOMAINS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def verify_story(story: Story) -> Story:
    """
    Apply the final verification gate based on cluster confidence.

    Raises StoryRejected if:
      - Source is Tier 5 (unreliable domain)
      - Confidence is REJECT (no Tier 1–3 source in cluster)

    Sets story.verification_status:
      - HIGH / GOOD confidence → VERIFIED
      - LOW confidence         → UNVERIFIED (score capped at 55 by scorer)
    """
    # Hard reject — Tier 5 source
    domain = _domain(story.source_url)
    if domain in TIER5_DOMAINS:
        story.verification_status = VerificationStatus.SPECULATIVE
        raise StoryRejected(f"Source domain '{domain}' is on the unreliable list.")

    # Hard reject — source tier 5 by classification
    source_tier = getattr(story, "source_tier", 4)
    if source_tier == SourceTier.TIER5 or source_tier == 5:
        story.verification_status = VerificationStatus.SPECULATIVE
        raise StoryRejected(f"Source '{story.source_name}' classified as Tier 5 (unreliable).")

    confidence = getattr(story, "confidence", Confidence.LOW)
    corr_count = len(story.corroborating_sources or [])

    logger.info(
        "Verification: [%s | T%s | %d corroborating] '%s'",
        confidence,
        source_tier,
        corr_count,
        story.title,
    )

    if confidence == Confidence.REJECT:
        story.verification_status = VerificationStatus.UNVERIFIED
        raise StoryRejected(
            f"No reliable source in cluster for: {story.title}"
        )

    if confidence in (Confidence.HIGH, Confidence.GOOD):
        story.verification_status = VerificationStatus.VERIFIED
        logger.info(
            "✅ VERIFIED [%s] %d source(s): %s",
            confidence, corr_count + 1, story.title[:70],
        )
    else:
        # LOW confidence — allow through but flag as UNVERIFIED
        # Editorial scorer will cap these at 55 (below PUBLISH floor)
        story.verification_status = VerificationStatus.UNVERIFIED
        logger.warning(
            "⚠️  UNVERIFIED [LOW confidence | T%s]: %s",
            source_tier, story.title[:70],
        )

    return story


# ---------------------------------------------------------------------------
# Legacy shim — set_rss_pool kept for backwards compat (no-op now)
# ---------------------------------------------------------------------------

def set_rss_pool(stories: list[Story]) -> None:
    """
    No-op shim. RSS pool corroboration is now handled by the clusterer.
    Kept to avoid import errors in agent.py during transition.
    """
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    """Extract the root domain from a URL (e.g. 'bbc.com')."""
    try:
        hostname = urlparse(url).hostname or ""
        return hostname.removeprefix("www.")
    except Exception:
        return url
