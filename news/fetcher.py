"""
news/fetcher.py — Fetch recent worldwide news stories by category.

Primary source: NewsAPI top-headlines + everything
Fallback:       RSS feeds via feedparser
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import requests

import config
from models import NewsSourceError, Story

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, dict] = {
    "breaking": {
        "label": "Breaking / World News",
        "newsapi_category": "general",
        "keywords": "breaking news world",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.reuters.com/reuters/topNews",
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
    },
    "technology": {
        "label": "Technology & AI",
        "newsapi_category": "technology",
        "keywords": "technology artificial intelligence AI",
        "rss_feeds": [
            "https://feeds.feedburner.com/TechCrunch",
            "https://www.wired.com/feed/rss",
            "https://www.theverge.com/rss/index.xml",
        ],
    },
    "business": {
        "label": "Business & Economy",
        "newsapi_category": "business",
        "keywords": "business economy finance markets",
        "rss_feeds": [
            "https://feeds.bloomberg.com/markets/news.rss",
            "https://feeds.reuters.com/reuters/businessNews",
        ],
    },
    "politics": {
        "label": "Politics & Geopolitics",
        "newsapi_category": "general",
        "keywords": "politics geopolitics government elections",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/news/politics/rss.xml",
            "https://feeds.reuters.com/Reuters/PoliticsNews",
        ],
    },
    "science": {
        "label": "Science & Health",
        "newsapi_category": "health",
        "keywords": "science health medicine research",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/news/health/rss.xml",
            "https://www.sciencedaily.com/rss/top/science.xml",
        ],
    },
    "sports": {
        "label": "Sports",
        "newsapi_category": "sports",
        "keywords": "sports football basketball cricket",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/sport/rss.xml",
            "https://feeds.skysports.com/skysports/home",
        ],
    },
    "viral": {
        "label": "Trending / Viral",
        "newsapi_category": "general",
        "keywords": "trending viral popular social media",
        "rss_feeds": [
            "https://feeds.buzzfeed.com/buzzfeed/hot",
            "http://feeds.bbci.co.uk/news/rss.xml",
        ],
    },
    "entertainment": {
        "label": "Entertainment & Celebrities",
        "newsapi_category": "entertainment",
        "keywords": "entertainment celebrities movies music",
        "rss_feeds": [
            "https://variety.com/feed/",
            "https://www.tmz.com/rss.xml",
        ],
    },
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def fetch_news(limit: int = 5, category: str = "breaking") -> list[Story]:
    """
    Fetch the top `limit` recent news stories for the given category.
    Tries NewsAPI first; falls back to RSS if NewsAPI fails or returns nothing.

    Returns a list of Story objects sorted by published_at descending.
    Raises NewsSourceError if all sources fail.
    """
    cat = CATEGORIES.get(category, CATEGORIES["breaking"])
    logger.info("Fetching news for category: %s", cat["label"])

    stories: list[Story] = []

    try:
        stories = _fetch_from_newsapi(limit=limit, cat=cat)
        logger.info("NewsAPI returned %d stories for '%s'.", len(stories), cat["label"])
    except NewsSourceError as exc:
        logger.warning("NewsAPI failed (%s). Trying RSS fallback.", exc)

    if not stories:
        stories = _fetch_from_rss(limit=limit, cat=cat)
        logger.info("RSS fallback returned %d stories for '%s'.", len(stories), cat["label"])

    if not stories:
        raise NewsSourceError(f"No stories retrieved for category '{cat['label']}'.")

    stories.sort(key=lambda s: s.published_at, reverse=True)
    return stories[:limit]


def fetch_all_categories(limit_per_category: int = 2) -> list[Story]:
    """
    Fetch stories from all 8 categories and return a combined list.
    Useful for a full daily run across all topics.
    """
    all_stories: list[Story] = []
    for category in CATEGORIES:
        try:
            stories = fetch_news(limit=limit_per_category, category=category)
            for s in stories:
                s.category = category  # tag with category slug for image labelling
            all_stories.extend(stories)
        except NewsSourceError as exc:
            logger.warning("Could not fetch category '%s': %s", category, exc)
    return all_stories


# ---------------------------------------------------------------------------
# Primary: NewsAPI
# ---------------------------------------------------------------------------

def _fetch_from_newsapi(limit: int, cat: dict) -> list[Story]:
    """Call NewsAPI and return Story objects for the given category."""
    # Use top-headlines for categories that map to a NewsAPI category
    params = {
        "language": "en",
        "pageSize": limit,
        "apiKey": config.NEWSAPI_KEY,
    }

    newsapi_category = cat.get("newsapi_category")
    if newsapi_category and newsapi_category != "general":
        params["category"] = newsapi_category
        url = config.NEWSAPI_TOP_HEADLINES_URL
    else:
        # For general/politics/viral use keyword search for better results
        params["q"] = cat.get("keywords", "world news")
        params["sortBy"] = "publishedAt"
        url = config.NEWSAPI_EVERYTHING_URL

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NewsSourceError(f"NewsAPI request failed: {exc}") from exc

    data = response.json()

    if data.get("status") != "ok":
        raise NewsSourceError(
            f"NewsAPI returned error: {data.get('message', 'unknown error')}"
        )

    stories = []
    for article in data.get("articles", []):
        story = _article_to_story(article)
        if story:
            stories.append(story)

    return stories


def _article_to_story(article: dict) -> Story | None:
    """Convert a NewsAPI article dict to a Story. Returns None if the article is unusable."""
    title = (article.get("title") or "").strip()
    url = (article.get("url") or "").strip()

    if not title or not url or "[Removed]" in title:
        return None

    source = article.get("source") or {}
    source_name = (source.get("name") or "Unknown").strip()

    raw_summary = (
        article.get("description") or article.get("content") or ""
    ).strip()

    published_at = _parse_datetime(article.get("publishedAt"))

    return Story(
        title=title,
        source_name=source_name,
        source_url=url,
        published_at=published_at,
        raw_summary=raw_summary,
    )


# ---------------------------------------------------------------------------
# Fallback: RSS
# ---------------------------------------------------------------------------

def _fetch_from_rss(limit: int, cat: dict) -> list[Story]:
    """Try each RSS feed for the category and return stories from the first that succeeds."""
    for feed_url in cat.get("rss_feeds", config.RSS_FALLBACK_FEEDS):
        try:
            stories = _parse_rss_feed(feed_url, limit=limit)
            if stories:
                logger.info("RSS feed '%s' returned %d stories.", feed_url, len(stories))
                return stories
        except Exception as exc:  # noqa: BLE001
            logger.warning("RSS feed '%s' failed: %s", feed_url, exc)
    return []


def _parse_rss_feed(feed_url: str, limit: int = 5) -> list[Story]:
    """Parse an RSS feed and return up to `limit` Story objects."""
    feed = feedparser.parse(feed_url)

    stories = []
    for entry in feed.entries[:limit]:
        title = (getattr(entry, "title", "") or "").strip()
        url = (getattr(entry, "link", "") or "").strip()

        if not title or not url:
            continue

        raw_summary = (getattr(entry, "summary", "") or "").strip()

        published_at = datetime.now(timezone.utc)
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass

        source_name = (
            getattr(feed.feed, "title", None) or feed_url.split("/")[2]
        ).strip()

        stories.append(
            Story(
                title=title,
                source_name=source_name,
                source_url=url,
                published_at=published_at,
                raw_summary=raw_summary,
            )
        )

    return stories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_datetime(value: str | None) -> datetime:
    """Parse an ISO 8601 datetime string. Falls back to UTC now."""
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)
