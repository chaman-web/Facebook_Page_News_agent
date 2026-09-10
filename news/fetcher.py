"""
news/fetcher.py — Fetch recent worldwide news stories by category.

Sources (used in parallel, not fallback-only):
  1. NewsAPI top-headlines + everything
  2. RSS feeds (multiple per category)

Source health tracking:
  - A single timeout or HTTP error does not remove a source
  - Temporary cooldowns increase gradually after repeated failures
  - Important sources receive extra attempts and frequent recovery probes
  - Recent successful feed data covers short endpoint outages
  - A successful response restores the endpoint immediately

Always fetches a large pool then returns freshest `limit` stories.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests

import config
from models import NewsSourceError, Story
from pipeline.source_health import is_banned, record_failure, record_success
from news.regional_sources import (
    REGIONAL_FEEDS,
    discovery_priority,
    is_high_impact_candidate,
    is_region_relevant,
)

logger = logging.getLogger(__name__)

_FETCH_POOL = 30  # stories to fetch per individual source
_RSS_MAX_ATTEMPTS = 2
_FEED_CACHE_MAX_AGE = timedelta(hours=6)
_FEED_CACHE_DIR = config.PROJECT_ROOT / ".feed_cache"

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

def fetch_news(
    limit: int = 5,
    category: str = "breaking",
    include_regional: bool = True,
    _feed_cache: dict[str, list[Story]] | None = None,
) -> list[Story]:
    """
    Fetch `limit` fresh stories for the given category.
    - Pulls from NewsAPI + all healthy RSS feeds simultaneously
    - Temporarily cools repeatedly failing endpoints
    - Reuses recent cached results while an endpoint recovers
    - Returns freshest `limit` stories
    """
    cat = CATEGORIES.get(category, CATEGORIES["breaking"])
    logger.info("Fetching news for category: %s", cat["label"])

    pool: list[Story] = []
    seen_urls: set[str] = set()
    regional_candidates: list[Story] = []
    feed_cache = _feed_cache if _feed_cache is not None else {}

    def _add(stories: list[Story]) -> int:
        added = 0
        for s in stories:
            if _is_story_fresh(s) and s.source_url not in seen_urls:
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

    # 2. RSS feeds — isolate cooling to each endpoint and use recent cache
    rss_total = 0
    for feed_url in cat.get("rss_feeds", []):
        if feed_url in feed_cache:
            rss_stories = copy.deepcopy(feed_cache[feed_url])
            rss_total += _add(rss_stories)
            continue
        if is_banned(feed_url):
            continue
        rss_stories = _fetch_feed_resilient(feed_url, limit=10, context="Fetch")
        feed_cache[feed_url] = copy.deepcopy(rss_stories)
        if not rss_stories:
            continue
        added = _add(rss_stories)
        rss_total += added

    if rss_total:
        logger.info("RSS feeds added %d new stories for '%s'.", rss_total, cat["label"])

    # A direct breaking/world run must still cover every macro-region.  The
    # all-category path performs this sweep once after its category passes.
    if include_regional and category in {"breaking", "world"}:
        regional_candidates = fetch_regional_news(
            limit_per_region=min(max(2, limit // 10), 5),
            feed_cache=feed_cache,
        )
        _add(regional_candidates)

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
    result = pool[:fetch_limit]
    result_urls = {story.source_url for story in result}
    # Keep the regional quota even when a burst of newer US/UK headlines fills
    # the global freshness window.
    result.extend(
        story for story in regional_candidates
        if story.source_url not in result_urls
    )
    return result


def fetch_all_categories(limit_per_category: int = 2) -> list[Story]:
    """Fetch stories from all categories and return combined list."""
    all_stories: list[Story] = []
    feed_cache: dict[str, list[Story]] = {}
    for category in CATEGORIES:
        try:
            stories = fetch_news(
                limit=limit_per_category,
                category=category,
                include_regional=False,
                _feed_cache=feed_cache,
            )
            all_stories.extend(stories)
        except NewsSourceError as exc:
            logger.warning("Could not fetch category '%s': %s", category, exc)
    # Regional quotas prevent high-impact local stories from disappearing under
    # a globally sorted pool dominated by a handful of countries.
    all_stories.extend(
        fetch_regional_news(
            limit_per_region=min(max(3, limit_per_category // 5), 5),
            feed_cache=feed_cache,
        )
    )
    return all_stories


def fetch_regional_news(
    limit_per_region: int = 3,
    feed_cache: dict[str, list[Story]] | None = None,
) -> list[Story]:
    """Fetch a minimum candidate quota from every configured world region."""
    selected: list[Story] = []
    seen_urls: set[str] = set()
    coverage: dict[str, int] = {}
    run_cache = feed_cache if feed_cache is not None else {}

    for region, feeds in REGIONAL_FEEDS.items():
        candidates: list[Story] = []
        for feed_url in feeds:
            if feed_url not in run_cache:
                if is_banned(feed_url):
                    continue
                run_cache[feed_url] = _fetch_feed_resilient(
                    feed_url,
                    limit=max(30, limit_per_region * 5),
                    context="Regional fetch",
                )
            stories = copy.deepcopy(run_cache[feed_url])
            for story in stories:
                if (
                    _is_story_fresh(story)
                    and story.source_url not in seen_urls
                    and is_region_relevant(story, region)
                ):
                    story.region = region
                    story.category = "world"
                    candidates.append(story)
                    seen_urls.add(story.source_url)

        candidates.sort(key=discovery_priority, reverse=True)
        chosen = candidates[:limit_per_region]
        chosen_urls = {story.source_url for story in chosen}
        # The regional quota is a minimum sample, never a ceiling for major
        # local developments. Preserve every additional impact candidate for
        # normal clustering, verification and editorial scoring.
        chosen.extend(
            story for story in candidates[limit_per_region:]
            if story.source_url not in chosen_urls and is_high_impact_candidate(story)
        )
        selected.extend(chosen)
        coverage[region] = len(chosen)

        if len(chosen) < limit_per_region:
            logger.warning(
                "REGIONAL COVERAGE ALERT: %s returned %d/%d expected candidates.",
                region,
                len(chosen),
                limit_per_region,
            )

    logger.info(
        "Regional discovery added %d candidates: %s",
        len(selected),
        ", ".join(f"{region}={count}" for region, count in coverage.items()),
    )
    return selected


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
    if published_at is None:
        logger.debug("Skipping undated NewsAPI story: %s", title[:100])
        return None
    story = Story(
        title=title, source_name=source_name, source_url=url,
        published_at=published_at, raw_summary=raw_summary,
    )
    return story if _is_story_fresh(story) else None


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def _parse_rss_feed(feed_url: str, limit: int = 10) -> list[Story]:
    response = None
    for attempt in range(1, _RSS_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                feed_url,
                timeout=12,
                headers={"User-Agent": "GlobalPulseNews/1.0 (+news-feed-reader)"},
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt >= _RSS_MAX_ATTEMPTS or not _is_transient_request_error(exc):
                raise
            delay = 0.2 * attempt + random.uniform(0.05, 0.2)
            logger.info(
                "Transient RSS error; retrying endpoint in %.2fs (%d/%d) — %s",
                delay,
                attempt + 1,
                _RSS_MAX_ATTEMPTS,
                feed_url,
            )
            time.sleep(delay)

    if response is None:
        return []
    feed = feedparser.parse(response.content)
    stories = []
    for entry in feed.entries[:limit]:
        title = (getattr(entry, "title", "") or "").strip()
        url   = (getattr(entry, "link",  "") or "").strip()
        if not title or not url:
            continue
        raw_summary  = (getattr(entry, "summary", "") or "").strip()
        published_at = None
        for date_field in ("published_parsed", "updated_parsed"):
            parsed_value = getattr(entry, date_field, None)
            if not parsed_value:
                continue
            try:
                published_at = datetime(*parsed_value[:6], tzinfo=timezone.utc)
                break
            except (TypeError, ValueError):
                continue
        if published_at is None:
            for date_field in ("published", "updated", "date"):
                published_at = _parse_datetime(getattr(entry, date_field, None))
                if published_at is not None:
                    break
        if published_at is None:
            logger.debug("Skipping RSS story without a trustworthy date: %s", title[:100])
            continue
        source_name = (
            getattr(feed.feed, "title", None) or feed_url.split("/")[2]
        ).strip()
        story = Story(
            title=title, source_name=source_name, source_url=url,
            published_at=published_at, raw_summary=raw_summary,
        )
        # Keep dated entries here so a healthy but quiet feed is not recorded as
        # failed. Discovery and cache loading apply the hard freshness gate.
        stories.append(story)
    return stories


def _fetch_feed_resilient(feed_url: str, limit: int, context: str) -> list[Story]:
    """Fetch one endpoint, falling back to recent cached stories on failure."""
    try:
        stories = _parse_rss_feed(feed_url, limit=limit)
        if not stories:
            raise NewsSourceError("Endpoint returned no usable RSS entries")
        record_success(feed_url)
        if stories:
            _save_feed_cache(feed_url, stories)
        return stories
    except Exception as exc:
        reason = f"{context} error: {type(exc).__name__}: {str(exc)[:180]}"
        record_failure(feed_url, reason)
        cached = _load_feed_cache(feed_url, limit)
        if cached:
            logger.warning(
                "Using recent cached feed during endpoint outage — %s | %d stories",
                feed_url,
                len(cached),
            )
        return cached


def _is_transient_request_error(exc: requests.RequestException) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status == 429 or bool(status and status >= 500)


def _cache_path(feed_url: str) -> Path:
    name = hashlib.sha256(feed_url.encode("utf-8", errors="ignore")).hexdigest()
    return _FEED_CACHE_DIR / f"{name}.json"


def _save_feed_cache(feed_url: str, stories: list[Story]) -> None:
    try:
        _FEED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "feed_url": feed_url,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "stories": [
                {
                    "title": story.title,
                    "source_name": story.source_name,
                    "source_url": story.source_url,
                    "published_at": story.published_at.isoformat(),
                    "raw_summary": story.raw_summary,
                }
                for story in stories
            ],
        }
        _cache_path(feed_url).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("Could not save feed cache for %s: %s", feed_url, exc)


def _load_feed_cache(feed_url: str, limit: int) -> list[Story]:
    path = _cache_path(feed_url)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(payload["saved_at"])
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - saved_at > _FEED_CACHE_MAX_AGE:
            return []
        stories = []
        for item in payload.get("stories", [])[:limit]:
            published_at = datetime.fromisoformat(item["published_at"])
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            story = Story(
                title=item["title"],
                source_name=item["source_name"],
                source_url=item["source_url"],
                published_at=published_at,
                raw_summary=item.get("raw_summary", ""),
            )
            if _is_story_fresh(story):
                stories.append(story)
        return stories
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_story_fresh(story: Story, now: datetime | None = None) -> bool:
    """Return true only when a story is within the configured news window."""
    reference = now or datetime.now(timezone.utc)
    published_at = story.published_at
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age = reference - published_at.astimezone(timezone.utc)
    return age <= timedelta(hours=config.NEWS_MAX_AGE_HOURS)
