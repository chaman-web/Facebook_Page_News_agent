"""
pipeline/clusterer.py — Story clustering and primary source selection.

Groups articles reporting the same event into clusters, then:
  1. Selects the highest-tier source as the primary story
  2. Computes cluster confidence from all source weights
  3. Rejects clusters with no Tier 1–3 primary source

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CLUSTERING ALGORITHM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Two stories are clustered together if ALL of the following:
  1. Published within CLUSTER_TIME_WINDOW_HOURS of each other
  2. Title similarity ≥ TITLE_SIMILARITY_THRESHOLD (difflib ratio)
     OR shared named entity count ≥ ENTITY_OVERLAP_THRESHOLD

Contradiction check:
  If two stories in the same cluster contain contradictory keywords
  (e.g. "ceasefire" vs "offensive", "wins" vs "loses"),
  they are NOT merged — kept as separate events.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  cluster_stories() → list of Story (one per cluster, best source as primary)
  Each returned Story has:
    story.source_tier     — Tier of the primary source
    story.cluster_size    — Number of articles in the cluster
    story.confidence      — HIGH / GOOD / LOW / REJECT
    story.corroborating_sources — all other sources in the cluster
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher

from models import Story, StoryRejected
from pipeline.source_classifier import (
    Confidence,
    SourceTier,
    classify_source,
    compute_confidence,
    canonical_domain,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

CLUSTER_TIME_WINDOW_HOURS  = 12      # stories within 12h may be same event
TITLE_SIMILARITY_THRESHOLD = 0.45   # difflib ratio threshold for same-event
ENTITY_OVERLAP_THRESHOLD   = 3      # shared named entity tokens to auto-cluster

# Contradiction pairs — if both sides appear in same cluster, don't merge
_CONTRADICTION_PAIRS: list[tuple[str, str]] = [
    (r"\bceasefire\b",   r"\boffensive\b"),
    (r"\bceasefire\b",   r"\battack\b"),
    (r"\bwins?\b",       r"\blose[sd]?\b"),
    (r"\bagreed?\b",     r"\brejected?\b"),
    (r"\bapproved?\b",   r"\bblocked?\b"),
    (r"\blive[sd]?\b",   r"\bdie[sd]?\b|dead\b"),
    (r"\brelease[sd]?\b",r"\barrested?\b"),
    (r"\badvances?\b",   r"\bretreats?\b"),
    (r"\bopen[sed]?\b",  r"\bclosed?\b"),
    (r"\bconfirmed?\b",  r"\bdenied?\b"),
]

# Stopwords excluded from entity/keyword extraction
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "over", "after",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "its", "it", "this", "that", "these", "those", "he", "she", "they",
    "we", "you", "i", "his", "her", "their", "our", "my", "your",
    "says", "said", "say", "report", "reports", "reported",
    "news", "breaking", "latest", "update", "new", "first",
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def cluster_stories(stories: list[Story]) -> list[Story]:
    """
    Cluster stories by event, select best primary source per cluster,
    compute confidence, reject weak clusters.

    Returns one Story per cluster (the primary source story), enriched with:
      - source_tier, cluster_size, confidence, corroborating_sources
    """
    if not stories:
        return []

    # Step 1: classify every story's source
    for story in stories:
        classification = classify_source(story.source_name, story.source_url)
        story.source_tier = classification.tier

        # Reject Tier 5 sources immediately
        if classification.tier == SourceTier.TIER5:
            logger.debug("⛔ Tier 5 source rejected: %s | %s", story.source_name, story.title[:50])
            story.confidence = Confidence.REJECT

    # Filter out Tier 5
    stories = [s for s in stories if getattr(s, "confidence", None) != Confidence.REJECT]

    # Step 2: build clusters
    clusters: list[list[Story]] = _build_clusters(stories)

    logger.info(
        "Clusterer: %d articles → %d event clusters",
        len(stories), len(clusters),
    )

    # Step 3: process each cluster
    results: list[Story] = []
    rejected = 0

    for cluster in clusters:
        primary = _select_primary(cluster)
        if primary is None:
            rejected += 1
            continue

        # Compute confidence from all sources in the cluster
        classifications = [
            classify_source(s.source_name, s.source_url) for s in cluster
        ]
        confidence = compute_confidence(classifications)

        if confidence == Confidence.REJECT:
            logger.debug(
                "⛔ Cluster rejected (no reliable source): %s",
                primary.title[:65],
            )
            rejected += 1
            continue

        # Enrich primary story with cluster data
        primary.cluster_size = len(cluster)
        primary.confidence   = confidence

        # Build corroborating_sources from other cluster members
        corroborating = []
        for s in cluster:
            if s.source_url != primary.source_url:
                corroborating.append({
                    "name": s.source_name,
                    "url":  s.source_url,
                    "tier": getattr(s, "source_tier", SourceTier.TIER4),
                    "domain": canonical_domain(s.source_url),
                    "title": s.title,
                    "summary": s.raw_summary or "",
                    "published_at": s.published_at.isoformat() if s.published_at else None,
                })
        primary.corroborating_sources = corroborating

        results.append(primary)

        logger.debug(
            "✅ Cluster [%s | T%d | %d sources]: %s",
            confidence,
            primary.source_tier,
            len(cluster),
            primary.title[:65],
        )

    logger.info(
        "Clustering: %d clusters → %d publishable | %d rejected (no reliable source)",
        len(clusters), len(results), rejected,
    )

    return results


# ---------------------------------------------------------------------------
# Clustering logic
# ---------------------------------------------------------------------------

def _build_clusters(stories: list[Story]) -> list[list[Story]]:
    """
    Greedy single-pass clustering.
    Each story is assigned to the first cluster it matches,
    or starts a new cluster if it doesn't match any.
    """
    clusters: list[list[Story]] = []

    for story in stories:
        assigned = False
        for cluster in clusters:
            # Use the first story in the cluster as the representative
            rep = cluster[0]
            if _same_event(story, rep):
                cluster.append(story)
                assigned = True
                break
        if not assigned:
            clusters.append([story])

    return clusters


def _same_event(a: Story, b: Story) -> bool:
    """
    Returns True if two stories are likely reporting the same event.

    Criteria (all must pass):
      1. Published within CLUSTER_TIME_WINDOW_HOURS of each other
      2. Title similarity ≥ threshold OR entity overlap ≥ threshold
      3. No contradiction detected between titles
    """
    # 1. Time window
    ta = a.published_at
    tb = b.published_at
    # Make both timezone-aware/naive consistently
    try:
        if ta.tzinfo is None and tb.tzinfo is not None:
            from datetime import timezone
            ta = ta.replace(tzinfo=timezone.utc)
        elif ta.tzinfo is not None and tb.tzinfo is None:
            from datetime import timezone
            tb = tb.replace(tzinfo=timezone.utc)
        if abs((ta - tb).total_seconds()) > CLUSTER_TIME_WINDOW_HOURS * 3600:
            return False
    except Exception:
        pass

    title_a = a.title.lower()
    title_b = b.title.lower()

    # 2a. Title similarity
    similarity = SequenceMatcher(None, title_a, title_b).ratio()
    if similarity >= TITLE_SIMILARITY_THRESHOLD:
        # 3. Contradiction check before confirming same event
        if _contradicts(title_a, title_b):
            return False
        return True

    # 2b. Entity/keyword overlap
    entities_a = _extract_entities(title_a)
    entities_b = _extract_entities(title_b)
    overlap    = len(entities_a & entities_b)
    if overlap >= ENTITY_OVERLAP_THRESHOLD:
        if _contradicts(title_a, title_b):
            return False
        return True

    return False


def _contradicts(text_a: str, text_b: str) -> bool:
    """
    Returns True if the two texts contain contradictory signals.
    E.g. one says "ceasefire agreed" and the other says "offensive launched".
    """
    combined = text_a + " " + text_b
    for pattern_a, pattern_b in _CONTRADICTION_PAIRS:
        if (re.search(pattern_a, text_a, re.IGNORECASE) and
                re.search(pattern_b, text_b, re.IGNORECASE)):
            return True
        if (re.search(pattern_b, text_a, re.IGNORECASE) and
                re.search(pattern_a, text_b, re.IGNORECASE)):
            return True
    return False


def _extract_entities(text: str) -> set[str]:
    """
    Extract significant tokens (≥4 chars, not stopwords) from title.
    Used for entity-overlap clustering.
    Capitalised words get extra weight as they're likely named entities.
    """
    # Extract all words ≥ 4 chars
    words  = re.findall(r"\b[a-zA-Z]{4,}\b", text)
    tokens = {w.lower() for w in words if w.lower() not in _STOPWORDS}

    # Also extract numbers (casualty counts, dates, years)
    numbers = set(re.findall(r"\b\d{2,}\b", text))

    return tokens | numbers


# ---------------------------------------------------------------------------
# Primary source selection
# ---------------------------------------------------------------------------

def _select_primary(cluster: list[Story]) -> Story | None:
    """
    Select the best story from a cluster to be the primary story.

    Priority:
      1. Lowest tier number (Tier 1 beats Tier 2, etc.)
      2. Among same tier, pick most recently published
      3. Tier 4/5 only clusters → return None (no publishable primary)
    """
    # Filter to primary-eligible sources only (Tier 1–3)
    eligible = [
        s for s in cluster
        if getattr(s, "source_tier", SourceTier.TIER4).can_be_primary
    ]

    if not eligible:
        return None

    # Sort: lowest tier first, then most recent
    eligible.sort(
        key=lambda s: (
            int(getattr(s, "source_tier", SourceTier.TIER4)),
            -s.published_at.timestamp() if s.published_at else 0,
        )
    )

    return eligible[0]
