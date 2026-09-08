"""Curated regional discovery feeds and fair-coverage helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from models import Story


# Each region has at least one broad regional feed and, where available, a
# reputable local publisher.  The same URL may appear in a category pool; URL
# deduplication later in the pipeline removes the overlap.
REGIONAL_FEEDS: dict[str, tuple[str, ...]] = {
    "pakistan": (
        "https://www.dawn.com/feeds/home",
        "https://geo.tv/rss/10",
        "https://www.thenews.com.pk/rss/1/1",
    ),
    "india": (
        "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "https://feeds.feedburner.com/ndtvnews-top-stories",
        "https://www.thehindu.com/feeder/default.rss",
        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    ),
    "gulf_ksa_uae": (
        "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "https://www.arabnews.com/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ),
    "middle_east": (
        "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ),
    "turkey": (
        "https://www.dailysabah.com/rssfeed/12/2",
        "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    ),
    "europe": (
        "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
        "https://www.theguardian.com/world/europe-news/rss",
    ),
    "east_southeast_asia": (
        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
        "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
    ),
    "africa": (
        "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
        "https://www.africanews.com/feed/rss",
    ),
    "north_america": (
        "https://www.cbc.ca/cmlink/rss-topstories",
        "https://feeds.npr.org/1001/rss.xml",
    ),
    "latin_america": (
        "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml",
    ),
    "oceania": (
        "https://www.abc.net.au/news/feed/51120/rss.xml",
        "https://www.theguardian.com/australia-news/rss",
    ),
    "official_global": (
        "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
    ),
}

REGIONAL_TERMS: dict[str, re.Pattern[str]] = {
    "pakistan": re.compile(
        r"\b(?:pakistan|pakistani|islamabad|karachi|lahore|peshawar|quetta|punjab|sindh|balochistan|kashmir)\b", re.I
    ),
    "india": re.compile(
        r"\b(?:india|indian|new delhi|delhi|mumbai|bengaluru|kolkata|chennai|modi|kashmir)\b", re.I
    ),
    "gulf_ksa_uae": re.compile(
        r"\b(?:gulf|uae|emirates|dubai|abu dhabi|saudi|riyadh|ksa|qatar|doha|oman|bahrain|kuwait)\b", re.I
    ),
    "middle_east": re.compile(
        r"\b(?:middle east|gulf|uae|emirates|dubai|abu dhabi|saudi|riyadh|ksa|qatar|doha|"
        r"iran|iraq|israel|gaza|palestin|yemen|oman|bahrain|kuwait|jordan|lebanon|syria)\b", re.I
    ),
    "turkey": re.compile(
        r"\b(?:turkey|türkiye|turkish|ankara|istanbul|erdogan)\b", re.I
    ),
    "europe": re.compile(
        r"\b(?:europe|european|eu|ukraine|russia|"
        r"germany|france|britain|uk|italy|spain|poland|greece|balkans|brussels)\b", re.I
    ),
    "east_southeast_asia": re.compile(
        r"\b(?:china|chinese|japan|japanese|korea|taiwan|hong kong|singapore|malaysia|"
        r"indonesia|thailand|philippines|vietnam|myanmar|cambodia|laos|asia-pacific)\b", re.I
    ),
    "africa": re.compile(
        r"\b(?:africa|african|nigeria|kenya|ethiopia|sudan|somalia|south africa|egypt|"
        r"libya|tunisia|algeria|morocco|congo|tanzania|uganda|ghana|sahel|zimbabwe)\b", re.I
    ),
    "north_america": re.compile(
        r"\b(?:united states|u\.s\.|us |america|american|canada|canadian|mexico|mexican)\b", re.I
    ),
    "latin_america": re.compile(
        r"\b(?:latin america|south america|brazil|argentina|colombia|venezuela|chile|peru|"
        r"ecuador|bolivia|uruguay|paraguay|cuba|haiti|panama|guatemala)\b", re.I
    ),
    "oceania": re.compile(
        r"\b(?:australia|australian|new zealand|pacific islands?|fiji|papua new guinea|tonga|samoa)\b", re.I
    ),
}


_DISCOVERY_IMPACT = re.compile(
    r"\b(?:earthquake|tsunami|cyclone|hurricane|typhoon|flood|wildfire|"
    r"war|invasion|missile|attack|ceasefire|coup|election|emergency|"
    r"outbreak|epidemic|pandemic|evacuat|killed|dead|missing|sanction|"
    r"interest rate|inflation|recession|currency|bank|shutdown|blackout|"
    r"court|arrested|indicted|resigns?|prime minister|president)\b",
    re.IGNORECASE,
)


def discovery_priority(story: Story) -> tuple[int, float]:
    """Sort regional candidates by impact first and freshness second."""
    text = f"{story.title} {story.raw_summary}"
    impact_hits = len(_DISCOVERY_IMPACT.findall(text))
    published = story.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    return impact_hits, -age_hours


def is_region_relevant(story: Story, region: str) -> bool:
    """Check that a regional-feed story is about that region, not merely hosted there."""
    if region == "official_global":
        return True
    matcher = REGIONAL_TERMS.get(region)
    text = f"{story.title} {story.raw_summary}"
    return bool(matcher and matcher.search(text))
