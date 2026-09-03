"""
pipeline/verifier.py — Cross-source verification of a news story.

Queries NewsAPI a second time using keywords from the story title to find
corroborating sources. Sets the story's verification_status accordingly.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

import config
from models import Story, StoryRejected, VerificationStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known unreliable domains (extend as needed)
# ---------------------------------------------------------------------------

UNRELIABLE_DOMAINS: set[str] = {
    "beforeitsnews.com",
    "yournewswire.com",
    "newspunch.com",
    "naturalnews.com",
    "infowars.com",
    "globalresearch.ca",
    "zerohedge.com",
    "thegatewaypundit.com",
    "worldnewsdailyreport.com",
    "empirenews.net",
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def verify_story(story: Story) -> Story:
    """
    Assess how well a story is verified by searching for corroborating sources.

    Sets story.verification_status to VERIFIED, UNVERIFIED, or SPECULATIVE.
    Raises StoryRejected for SPECULATIVE stories or unreliable sources.
    Returns the updated Story.
    """
    original_domain = _domain(story.source_url)

    # Immediately reject stories from known unreliable sources
    if original_domain in UNRELIABLE_DOMAINS:
        story.verification_status = VerificationStatus.SPECULATIVE
        raise StoryRejected(
            f"Source domain '{original_domain}' is on the unreliable list."
        )

    keywords = _extract_keywords(story.title)
    corroborating = _search_corroborating(keywords, exclude_domain=original_domain)

    story.corroborating_sources = corroborating

    source_count = len(corroborating)
    logger.info(
        "Verification: found %d corroborating source(s) for '%s'",
        source_count,
        story.title,
    )

    if source_count >= config.NEWS_MIN_SOURCES:
        story.verification_status = VerificationStatus.VERIFIED
    elif source_count == 1:
        story.verification_status = VerificationStatus.UNVERIFIED
        logger.warning("Story is UNVERIFIED (only 1 corroborating source): %s", story.title)
    else:
        # 0 corroborating sources — mark UNVERIFIED (not SPECULATIVE unless source is bad)
        story.verification_status = VerificationStatus.UNVERIFIED
        logger.warning("Story is UNVERIFIED (no corroborating sources found): %s", story.title)

    return story


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _search_corroborating(keywords: str, exclude_domain: str) -> list[dict]:
    """
    Search NewsAPI /v2/everything for the keywords.
    Returns a list of {name, url} dicts from distinct domains,
    excluding the original source domain.
    """
    if not keywords:
        return []

    params = {
        "q": keywords,
        "language": "en",
        "pageSize": 10,
        "sortBy": "relevancy",
        "apiKey": config.NEWSAPI_KEY,
    }

    try:
        response = requests.get(
            config.NEWSAPI_EVERYTHING_URL,
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning("Corroboration search failed: %s", exc)
        return []

    if data.get("status") != "ok":
        logger.warning("NewsAPI corroboration error: %s", data.get("message"))
        return []

    seen_domains: set[str] = {exclude_domain}
    corroborating: list[dict] = []

    for article in data.get("articles", []):
        url = (article.get("url") or "").strip()
        name = ((article.get("source") or {}).get("name") or "").strip()
        if not url or not name:
            continue

        domain = _domain(url)
        if domain in seen_domains:
            continue  # Skip duplicate domains

        seen_domains.add(domain)
        corroborating.append({"name": name, "url": url})

    return corroborating


def _extract_keywords(title: str) -> str:
    """
    Extract the 4 most significant words from a title for use as a search query.
    Strips common stop words and punctuation.
    """
    STOP_WORDS = {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "is", "are", "was", "were",
        "has", "have", "had", "it", "its", "as", "be", "been", "this",
        "that", "says", "said", "will", "over", "after", "amid",
    }
    words = re.sub(r"[^\w\s]", "", title).split()
    keywords = [w for w in words if w.lower() not in STOP_WORDS and len(w) > 2]
    return " ".join(keywords[:4])


def _domain(url: str) -> str:
    """Extract the root domain from a URL (e.g. 'bbc.com')."""
    try:
        hostname = urlparse(url).hostname or ""
        # Strip leading 'www.'
        return hostname.removeprefix("www.")
    except Exception:  # noqa: BLE001
        return url
