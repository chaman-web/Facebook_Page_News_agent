"""Curated regional discovery feeds and fair-coverage helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        "https://arynews.tv/feed/",
        "https://tribune.com.pk/feed/home",
        "https://www.app.com.pk/feed/",
    ),
    "india": (
        "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "https://feeds.feedburner.com/ndtvnews-top-stories",
        "https://www.thehindu.com/feeder/default.rss",
        "https://indianexpress.com/feed/",
        "https://www.hindustantimes.com/feeds/rss/india-news/rssfeed.xml",
        "https://theprint.in/feed/",
        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    ),
    "south_asia": (
        "https://www.thedailystar.net/frontpage/rss.xml",
        "https://kathmandupost.com/rss",
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
        "https://www.timesofisrael.com/feed/",
        "https://www.dailystar.com.lb/RSS.aspx",
    ),
    "turkey": (
        "https://www.dailysabah.com/rssfeed/12/2",
        "https://www.hurriyetdailynews.com/rss",
        "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    ),
    "europe": (
        "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
        "https://www.theguardian.com/world/europe-news/rss",
        "https://rss.dw.com/rdf/rss-en-all",
        "https://www.france24.com/en/rss",
        "https://www.euronews.com/rss?level=theme&name=news",
        "https://www.rte.ie/feeds/rss/?index=/news/",
    ),
    "east_southeast_asia": (
        "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
        "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
        "https://www.japantimes.co.jp/feed/topstories/",
        "https://www.scmp.com/rss/91/feed",
        "https://www.koreaherald.com/rss/newsAll",
        "https://www.rappler.com/feed/",
    ),
    "africa": (
        "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
        "https://www.africanews.com/feed/rss",
        "https://feeds.news24.com/articles/news24/TopStories/rss",
        "https://nation.africa/kenya/rss",
        "https://www.premiumtimesng.com/feed",
        "https://english.ahram.org.eg/UI/Front/RSS.aspx",
    ),
    "north_america": (
        "https://www.cbc.ca/cmlink/rss-topstories",
        "https://feeds.npr.org/1001/rss.xml",
        "https://globalnews.ca/feed/",
    ),
    "latin_america": (
        "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml",
        "https://en.mercopress.com/rss",
        "https://buenosairesherald.com/feed",
    ),
    "oceania": (
        "https://www.abc.net.au/news/feed/51120/rss.xml",
        "https://www.theguardian.com/australia-news/rss",
        "https://www.rnz.co.nz/rss/national.xml",
        "https://www.sbs.com.au/news/topic/latest/feed",
    ),
    "official_global": (
        "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
        "https://www.who.int/rss-feeds/news-english.xml",
    ),
}

REGIONAL_TERMS: dict[str, re.Pattern[str]] = {
    "pakistan": re.compile(
        r"\b(?:pakistan|pakistani|islamabad|karachi|lahore|peshawar|quetta|punjab|sindh|balochistan|kashmir|"
        r"پاکستان|اسلام آباد|کراچی|لاہور|پشاور|کوئٹہ)\b", re.I
    ),
    "india": re.compile(
        r"\b(?:india|indian|new delhi|delhi|mumbai|bengaluru|kolkata|chennai|modi|kashmir|"
        r"भारत|इंडिया|दिल्ली|मुंबई)\b", re.I
    ),
    "south_asia": re.compile(
        r"\b(?:bangladesh|bangladeshi|dhaka|nepal|nepali|kathmandu|sri lanka|sri lankan|colombo|"
        r"bhutan|maldives|maldivian|afghanistan|afghan|kabul)\b", re.I
    ),
    "gulf_ksa_uae": re.compile(
        r"\b(?:gulf|uae|emirates|dubai|abu dhabi|saudi|riyadh|ksa|qatar|doha|oman|bahrain|kuwait|"
        r"السعودية|الإمارات|دبي|قطر|الكويت|البحرين|عمان)\b", re.I
    ),
    "middle_east": re.compile(
        r"\b(?:middle east|gulf|uae|emirates|dubai|abu dhabi|saudi|riyadh|ksa|qatar|doha|"
        r"iran|iraq|israel|gaza|palestin|yemen|oman|bahrain|kuwait|jordan|lebanon|syria|"
        r"إيران|العراق|إسرائيل|فلسطين|غزة|اليمن|سوريا|لبنان)\b", re.I
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


@dataclass(frozen=True)
class RegionalImpact:
    score: float
    reasons: tuple[str, ...]

    @property
    def is_major(self) -> bool:
        return self.score >= 8 and len(self.reasons) >= 2


_REGIONAL_IMPACT_DIMENSIONS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("human-safety", 4.0, (
        r"\b\d[\d,]*\s+(?:people\s+)?(?:killed|dead|injured|missing|displaced|evacuated)",
        r"\b(?:mass casualties|deadly (?:flood|quake|attack|fire|crash)|death toll)\b",
    )),
    ("emergency-response", 3.0, (
        r"\b(?:state|national|public health) (?:of )?emergency\b",
        r"\b(?:mass evacuation|emergency declared|disaster response|rescue operation)\b",
    )),
    ("essential-services", 3.0, (
        r"\b(?:blackout|power outage|water shortage|internet shutdown|airport closure|schools? closed)\b",
        r"\b(?:hospital|power grid|transport network|banking system)s?\b.{0,70}\b(?:closed|halted|disrupted|failed|shutdown)\b",
    )),
    ("economic-policy", 3.0, (
        r"\b(?:central bank|government)\b.{0,80}\b(?:interest rate|currency|devalu|tariff|tax|subsid|capital controls)\b",
        r"\b(?:inflation|currency|debt|trade) crisis\b",
    )),
    ("leadership-or-election", 5.0, (
        r"\b(?:president|prime minister|government)\b.{0,80}\b(?:resigns?|removed|ousted|falls?|dissolved)\b",
        r"\b(?:wins?|loses?|annuls?|overturns?)\b.{0,60}\b(?:national |general |presidential )?election\b",
        r"\b(?:national |general |presidential )?election\b.{0,60}\b(?:result|winner|victory|defeat)\b",
    )),
    ("national-reach", 3.0, (
        r"\b(?:nationwide|countrywide|across the country|millions of people|multiple provinces|multiple states)\b",
        r"\b(?:national parliament|supreme court|constitutional court)\b.{0,80}\b(?:rules?|orders?|approves?|rejects?|overturns?)\b",
    )),
)

_MULTILINGUAL_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:زلزلہ|भूकंप|terremoto|deprem|séisme|زلزال)", re.I), "earthquake"),
    (re.compile(r"(?:سیلاب|बाढ़|inundaci[oó]n|sel|inondation|فيضانات)", re.I), "deadly flood"),
    (re.compile(r"(?:ہنگامی|आपात|emergencia|acil durum|urgence|طوارئ)", re.I), "state of emergency"),
    (re.compile(r"(?:جاں بحق|मौत|muertos?|[oö]ld[üu]|morts?|قتلى)", re.I), "death toll"),
    (re.compile(r"(?:انتخابات|चुनाव|elecciones|se[cç]im|élections)", re.I), "national election"),
    (re.compile(r"(?:بجلی بند|बिजली कटौती|apag[oó]n|elektrik kesintisi|coupure de courant)", re.I), "nationwide blackout"),
)


def _signal_text(story: Story) -> str:
    text = f"{story.title} {story.raw_summary}"
    translated_signals = [
        signal for pattern, signal in _MULTILINGUAL_SIGNALS if pattern.search(text)
    ]
    return f"{text} {' '.join(translated_signals)}"


def assess_regional_impact(story: Story) -> RegionalImpact:
    """Measure evidence of major local consequences without changing global stories."""
    region = getattr(story, "region", "global") or "global"
    if region in {"global", "official_global"}:
        return RegionalImpact(0.0, ())
    text = _signal_text(story)
    reasons: list[str] = []
    score = 0.0
    for label, points, patterns in _REGIONAL_IMPACT_DIMENSIONS:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            reasons.append(label)
            score += points
    return RegionalImpact(min(10.0, score), tuple(reasons))


def discovery_priority(story: Story) -> tuple[int, float]:
    """Sort regional candidates by impact first and freshness second."""
    text = _signal_text(story)
    impact_hits = len(_DISCOVERY_IMPACT.findall(text))
    published = story.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    regional = assess_regional_impact(story)
    return int(regional.score * 10) + impact_hits, -age_hours


def is_high_impact_candidate(story: Story) -> bool:
    """Protect locally important candidates from the regional sample limit."""
    text = _signal_text(story)
    return bool(_DISCOVERY_IMPACT.search(text)) or assess_regional_impact(story).score >= 4


def is_region_relevant(story: Story, region: str) -> bool:
    """Check that a regional-feed story is about that region, not merely hosted there."""
    if region == "official_global":
        return True
    matcher = REGIONAL_TERMS.get(region)
    text = f"{story.title} {story.raw_summary}"
    return bool(matcher and matcher.search(text))
