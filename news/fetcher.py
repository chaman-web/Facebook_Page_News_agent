"""
news/fetcher.py — Fetch recent worldwide news stories by category.

Sources (used in parallel, not fallback-only):
  1. NewsAPI top-headlines + everything
  2. RSS feeds (multiple per category)

Source health tracking:
  - If a feed returns rate-limit error → banned for 12h automatically
  - If a feed returns >= 80% duplicate stories → banned for 12h
  - If a feed fails (timeout, parse error) → banned for 12h
  - Banned sources are skipped automatically, tried again after 12h

Always fetches a large pool then returns freshest `limit` stories.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import requests

import config
from models import NewsSourceError, Story
from pipeline.source_health import ban, is_banned, record_duplicates

logger = logging.getLogger(__name__)

_FETCH_POOL = 30  # stories to fetch per individual source

# ---------------------------------------------------------------------------
# Category definitions — 6+ RSS feeds per category for rotation
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
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://feeds.skynews.com/feeds/rss/world.xml",
            "https://www.theguardian.com/world/rss",
            "https://abcnews.go.com/abcnews/topstories",
            "https://feeds.npr.org/1001/rss.xml",
            "https://www.cbsnews.com/latest/rss/main",
            "https://feeds.washingtonpost.com/rss/world",
            # South Asia
            "https://www.dawn.com/feeds/home",
            "https://geo.tv/rss/10",
            "https://feeds.feedburner.com/ndtvnews-top-stories",
            "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
            "https://www.thehindu.com/feeder/default.rss",
            "https://www.thenews.com.pk/rss/1/1",
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
            "https://arstechnica.com/feed/",
            "https://feeds.feedburner.com/venturebeat/SZYF",
            "https://www.zdnet.com/news/rss.xml",
            "https://www.engadget.com/rss.xml",
            "https://thenextweb.com/feed/",
            "https://www.techradar.com/rss",
            "https://www.cnet.com/rss/news/",
            "https://feeds.macrumors.com/MacRumors-All",
            "https://9to5google.com/feed/",
        ],
    },
    "business": {
        "label": "Business & Economy",
        "newsapi_category": "business",
        "keywords": "business economy finance markets",
        "rss_feeds": [
            "https://feeds.reuters.com/reuters/businessNews",
            "https://feeds.bloomberg.com/markets/news.rss",
            "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            "https://www.forbes.com/business/feed/",
            "https://www.economist.com/business/rss.xml",
            "https://feeds.marketwatch.com/marketwatch/topstories/",
            "https://fortune.com/feed/",
            "https://hbr.org/feed",
            "https://feeds.ft.com/rss/home/uk",
            "https://www.businessinsider.com/rss",
        ],
    },
    "politics": {
        "label": "Politics & Geopolitics",
        "newsapi_category": "general",
        "keywords": "politics geopolitics government elections",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/news/politics/rss.xml",
            "https://feeds.reuters.com/Reuters/PoliticsNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
            "https://www.theguardian.com/politics/rss",
            "https://thehill.com/rss/syndicator/19109",
            "https://www.politico.com/rss/politicopicks.xml",
            "https://feeds.washingtonpost.com/rss/politics",
            "https://www.axios.com/feeds/feed.rss",
            "https://feeds.npr.org/1014/rss.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/Washington.xml",
            # South Asia politics
            "https://www.dawn.com/feeds/political",
            "https://feeds.feedburner.com/ndtvnews-india-news",
            "https://www.thehindu.com/news/national/feeder/default.rss",
        ],
    },
    "science": {
        "label": "Science & Health",
        "newsapi_category": "health",
        "keywords": "science health medicine research",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/news/health/rss.xml",
            "https://www.sciencedaily.com/rss/top/science.xml",
            "https://feeds.reuters.com/reuters/scienceNews",
            "https://www.newscientist.com/feed/home/",
            "https://www.nature.com/nature.rss",
            "https://feeds.feedburner.com/NatGeoNews",
            "https://www.science.org/rss/news_current.xml",
            "https://feeds.livescience.com/livescience/news",
            "https://phys.org/rss-feed/",
            "https://www.scientificamerican.com/feed/rss/",
        ],
    },
    "sports": {
        "label": "Sports",
        "newsapi_category": "sports",
        "keywords": "sports football basketball cricket",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/sport/rss.xml",
            "https://feeds.skysports.com/skysports/home",
            "https://www.espn.com/espn/rss/news",
            "https://api.foxsports.com/v1/rss",
            "https://www.sportingnews.com/us/rss",
            "https://bleacherreport.com/articles/feed",
            "https://www.cbssports.com/rss/headlines/",
            "https://rss.nytimes.com/services/xml/rss/nyt/Sports.xml",
            "https://www.theguardian.com/sport/rss",
            "https://sports.yahoo.com/rss/",
        ],
    },
    "trending": {
        "label": "Trending / Viral",
        "newsapi_category": "general",
        "keywords": "trending viral popular social media",
        "rss_feeds": [
            "https://feeds.buzzfeed.com/buzzfeed/hot",
            "http://feeds.bbci.co.uk/news/rss.xml",
            "https://www.reddit.com/r/worldnews/.rss",
            "https://www.reddit.com/r/news/.rss",
            "https://mashable.com/feeds/rss/all",
            "https://www.buzzfeednews.com/feeds/buzzfeednews",
            "https://feeds.feedburner.com/boingboing/iBag",
            "https://www.mentalfloss.com/rss.xml",
        ],
    },
    "entertainment": {
        "label": "Entertainment & Celebrities",
        "newsapi_category": "entertainment",
        "keywords": "entertainment celebrities movies music",
        "rss_feeds": [
            "https://variety.com/feed/",
            "https://www.tmz.com/rss.xml",
            "https://www.hollywoodreporter.com/feed/",
            "https://deadline.com/feed/",
            "https://feeds.feedburner.com/EOnlineNews",
            "https://people.com/feed/",
            "https://www.rollingstone.com/feed/",
            "https://pitchfork.com/rss/news/feed/r.jss",
            "https://ew.com/feed/",
            "https://www.billboard.com/feed/",
        ],
    },
    "jobs": {
        "label": "Jobs & Opportunities",
        "newsapi_category": "business",
        "keywords": "jobs hiring employment careers workforce layoffs recruitment",
        "rss_feeds": [
            "https://feeds.reuters.com/reuters/businessNews",
            "https://www.theguardian.com/money/work-and-careers/rss",
            "https://feeds.feedburner.com/FastCompany",
            "https://www.cnbc.com/id/100727362/device/rss/rss.html",
            "https://hbr.org/jobs/rss",
            "https://feeds.feedburner.com/entrepreneur/latest",
            "https://www.businessinsider.com/careers/rss",
            "https://www.inc.com/rss",
            "https://fortune.com/feed/",
        ],
    },
    "world": {
        "label": "World News",
        "newsapi_category": "general",
        "keywords": "world international global news",
        "rss_feeds": [
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://feeds.reuters.com/reuters/topNews",
            "http://feeds.bbci.co.uk/news/world/rss.xml",
            "https://www.theguardian.com/world/rss",
            "https://feeds.skynews.com/feeds/rss/world.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://feeds.washingtonpost.com/rss/world",
            "https://abcnews.go.com/abcnews/internationalheadlines",
            "https://feeds.npr.org/1004/rss.xml",
            "https://www.cbsnews.com/latest/rss/world",
        ],
    },
    "crime": {
        "label": "CRIME & JUSTICE",
        "newsapi_category": "general",
        "keywords": "crime justice court arrest murder trial law police",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/news/uk/rss.xml",
            "https://feeds.reuters.com/reuters/domesticNews",
            "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
            "https://www.theguardian.com/uk/rss",
            "https://feeds.skynews.com/feeds/rss/uk.xml",
            "https://nypost.com/feed/",
            "https://feeds.washingtonpost.com/rss/national",
            "https://abcnews.go.com/abcnews/usheadlines",
            "https://www.cbsnews.com/latest/rss/us",
            "https://feeds.npr.org/1003/rss.xml",
        ],
    },
    "climate": {
        "label": "CLIMATE & ENVIRONMENT",
        "newsapi_category": "science",
        "keywords": "climate change environment global warming pollution renewable energy",
        "rss_feeds": [
            "https://www.theguardian.com/environment/rss",
            "https://feeds.reuters.com/reuters/environment",
            "https://e360.yale.edu/feed",
            "https://grist.org/feed/",
            "https://www.climatechangenews.com/feed/",
            "https://feeds.feedburner.com/NatGeoNews",
            "https://insideclimatenews.org/feed/",
            "https://www.carbonbrief.org/feed",
            "https://earthjustice.org/feeds/news",
            "https://www.eenews.net/rss/today.xml",
        ],
    },
    "war": {
        "label": "WAR & CONFLICT",
        "newsapi_category": "general",
        "keywords": "war conflict military attack battle ceasefire troops fighting",
        "rss_feeds": [
            "https://feeds.reuters.com/reuters/topNews",
            "http://feeds.bbci.co.uk/news/world/rss.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
            "https://feeds.skynews.com/feeds/rss/world.xml",
            "https://www.theguardian.com/world/rss",
            "https://feeds.washingtonpost.com/rss/world",
            "https://www.defensenews.com/rss/",
            "https://feeds.npr.org/1004/rss.xml",
            "https://theintercept.com/feed/?rss",
        ],
    },
    "wellness": {
        "label": "HEALTH & WELLNESS",
        "newsapi_category": "health",
        "keywords": "health wellness fitness mental health diet exercise wellbeing",
        "rss_feeds": [
            "http://feeds.bbci.co.uk/news/health/rss.xml",
            "https://www.theguardian.com/lifeandstyle/health-and-wellbeing/rss",
            "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",
            "https://feeds.webmd.com/rss/rss.aspx?RSSSource=RSS_PUBLIC",
            "https://www.healthline.com/rss/health-news",
            "https://feeds.reuters.com/reuters/healthNews",
            "https://www.medicalnewstoday.com/rss",
            "https://feeds.feedburner.com/psychologytoday",
            "https://www.health.com/feed",
            "https://consumer.healthday.com/rss-feeds.html",
        ],
    },
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def fetch_news(limit: int = 5, category: str = "breaking") -> list[Story]:
    """
    Fetch `limit` fresh stories for the given category.
    - Pulls from NewsAPI + all healthy RSS feeds simultaneously
    - Skips banned sources automatically
    - Bans sources that rate-limit or produce mostly duplicates
    - Returns freshest `limit` stories
    """
    cat = CATEGORIES.get(category, CATEGORIES["breaking"])
    logger.info("Fetching news for category: %s", cat["label"])

    pool: list[Story] = []
    seen_urls: set[str] = set()

    def _add(stories: list[Story]) -> int:
        added = 0
        for s in stories:
            if s.source_url not in seen_urls:
                seen_urls.add(s.source_url)
                pool.append(s)
                added += 1
        return added

    # 1. NewsAPI
    try:
        newsapi_stories = _fetch_from_newsapi(limit=_FETCH_POOL, cat=cat)
        added = _add(newsapi_stories)
        logger.info("NewsAPI returned %d stories for '%s'.", added, cat["label"])
    except NewsSourceError as exc:
        msg = str(exc)
        if "429" in msg or "rate" in msg.lower():
            logger.warning("NewsAPI rate-limited — using RSS sources only.")
        else:
            logger.warning("NewsAPI failed: %s", exc)

    # 2. RSS feeds — skip banned, ban failing ones
    rss_total = 0
    for feed_url in cat.get("rss_feeds", []):
        if is_banned(feed_url):
            continue
        try:
            rss_stories = _parse_rss_feed(feed_url, limit=10)
            if not rss_stories:
                continue
            before = len(pool)
            added  = _add(rss_stories)
            rss_total += added
            # Track duplicate ratio for this feed
            fetched    = len(rss_stories)
            duplicates = fetched - added
            record_duplicates(feed_url, fetched, duplicates)
        except Exception as exc:
            ban(feed_url, f"Fetch error: {type(exc).__name__}: {str(exc)[:120]}")

    if rss_total:
        logger.info("RSS feeds added %d new stories for '%s'.", rss_total, cat["label"])

    if not pool:
        raise NewsSourceError(f"No stories retrieved for category '{cat['label']}'.")

    pool.sort(key=lambda s: s.published_at, reverse=True)
    for s in pool:
        s.category = category

    fetch_limit = max(limit * 5, 20)   # return a larger pool for deduplication
    logger.info(
        "Total pool for '%s': %d stories. Returning top %d.",
        cat["label"], len(pool), min(fetch_limit, len(pool))
    )
    return pool[:fetch_limit]


def fetch_all_categories(limit_per_category: int = 2) -> list[Story]:
    """Fetch stories from all categories and return combined list."""
    all_stories: list[Story] = []
    for category in CATEGORIES:
        try:
            stories = fetch_news(limit=limit_per_category, category=category)
            all_stories.extend(stories)
        except NewsSourceError as exc:
            logger.warning("Could not fetch category '%s': %s", category, exc)
    return all_stories


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------

def _fetch_from_newsapi(limit: int, cat: dict) -> list[Story]:
    params = {
        "language": "en",
        "pageSize": min(limit, 100),
        "apiKey": config.NEWSAPI_KEY,
    }

    newsapi_category = cat.get("newsapi_category")
    if newsapi_category and newsapi_category != "general":
        params["category"] = newsapi_category
        url = config.NEWSAPI_TOP_HEADLINES_URL
    else:
        params["q"]      = cat.get("keywords", "world news")
        params["sortBy"] = "publishedAt"
        url = config.NEWSAPI_EVERYTHING_URL

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 429:
            raise NewsSourceError("429 rate limit")
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NewsSourceError(f"NewsAPI request failed: {exc}") from exc

    data = response.json()
    if data.get("status") != "ok":
        raise NewsSourceError(f"NewsAPI error: {data.get('message', 'unknown')}")

    stories = []
    for article in data.get("articles", []):
        story = _article_to_story(article)
        if story:
            stories.append(story)
    return stories


def _article_to_story(article: dict) -> Story | None:
    title = (article.get("title") or "").strip()
    url   = (article.get("url")   or "").strip()
    if not title or not url or "[Removed]" in title:
        return None
    source       = article.get("source") or {}
    source_name  = (source.get("name") or "Unknown").strip()
    raw_summary  = (article.get("description") or article.get("content") or "").strip()
    published_at = _parse_datetime(article.get("publishedAt"))
    return Story(
        title=title, source_name=source_name, source_url=url,
        published_at=published_at, raw_summary=raw_summary,
    )


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def _parse_rss_feed(feed_url: str, limit: int = 10) -> list[Story]:
    feed    = feedparser.parse(feed_url)
    stories = []
    for entry in feed.entries[:limit]:
        title = (getattr(entry, "title", "") or "").strip()
        url   = (getattr(entry, "link",  "") or "").strip()
        if not title or not url:
            continue
        raw_summary  = (getattr(entry, "summary", "") or "").strip()
        published_at = datetime.now(timezone.utc)
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
        source_name = (
            getattr(feed.feed, "title", None) or feed_url.split("/")[2]
        ).strip()
        stories.append(Story(
            title=title, source_name=source_name, source_url=url,
            published_at=published_at, raw_summary=raw_summary,
        ))
    return stories


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)
