"""
pipeline/source_classifier.py — Source quality classification for Global Pulse News.

Every story's publisher is assigned a tier immediately after fetch.
Tier determines whether a story can be a primary source, and how much
weight it contributes to cluster confidence scoring.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIER DEFINITIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Tier 1 — Wire services & global broadcasters
    Reuters, AP, AFP, BBC, Bloomberg, Al Jazeera, NPR
    → Can be primary. Confidence weight: 3.0
    → 1 Tier 1 source alone = GOOD confidence

  Tier 2 — Established national/international newspapers & broadcasters
    NYT, Guardian, Washington Post, FT, CNN, Sky News, ABC, NBC, CBS, etc.
    → Can be primary. Confidence weight: 2.0
    → 2 Tier 2 sources = GOOD confidence

  Tier 3 — Specialist / trade publications
    TechCrunch, Wired, Politico, Axios, Nature, New Scientist, ESPN, etc.
    → Can be primary within their domain. Confidence weight: 1.5
    → Alone: require Tier 1/2 confirmation

  Tier 4 — Blogs, aggregators, tabloids, regional unknowns
    BuzzFeed, TMZ, Reddit, HuffPost, aggregator sites, unknown domains
    → CANNOT be primary. Confidence weight: 0.5
    → Used only as corroboration signal; never as lead story

  Tier 5 — Social posts, unverified sources, known unreliable
    Known misinformation sites, social media posts, anonymous blogs
    → REJECT immediately; cannot corroborate
    → Confidence weight: 0.0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIDENCE SCORING (computed in clusterer.py)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  HIGH   (weight ≥ 6.0, or Tier1 + Tier1)  → VERIFIED  → full editorial score
  GOOD   (weight ≥ 3.0, or Tier1 alone)    → VERIFIED  → normal editorial score
  LOW    (weight ≥ 1.5, Tier3 alone)       → UNVERIFIED → score capped at 55
  REJECT (weight < 1.5, or no Tier1-3)     → StoryRejected

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------

class SourceTier(IntEnum):
    TIER1 = 1   # Wire services & global broadcasters
    TIER2 = 2   # Established national/international news
    TIER3 = 3   # Specialist / trade publications
    TIER4 = 4   # Blogs, aggregators, tabloids
    TIER5 = 5   # Unverified / known unreliable

    @property
    def label(self) -> str:
        return {
            SourceTier.TIER1: "Wire/Global",
            SourceTier.TIER2: "Established",
            SourceTier.TIER3: "Specialist",
            SourceTier.TIER4: "Aggregator/Blog",
            SourceTier.TIER5: "Unverified",
        }[self]

    @property
    def confidence_weight(self) -> float:
        return {
            SourceTier.TIER1: 3.0,
            SourceTier.TIER2: 2.0,
            SourceTier.TIER3: 1.5,
            SourceTier.TIER4: 0.5,
            SourceTier.TIER5: 0.0,
        }[self]

    @property
    def can_be_primary(self) -> bool:
        """Only Tier 1–3 sources can be the primary (lead) story source."""
        return self <= SourceTier.TIER3


# ---------------------------------------------------------------------------
# Confidence levels
# ---------------------------------------------------------------------------

class Confidence(str):
    HIGH   = "HIGH"    # Reuters + AP + BBC → publish immediately
    GOOD   = "GOOD"    # BBC + established local → publish normally
    LOW    = "LOW"     # Single Tier 3 → HOLD filler only
    REJECT = "REJECT"  # No reliable source → reject


# Thresholds
CONFIDENCE_HIGH   = 6.0   # e.g. Reuters (3) + AP (3)
CONFIDENCE_GOOD   = 3.0   # e.g. Reuters alone (3), or NYT (2) + Guardian (2) - 1
CONFIDENCE_LOW    = 1.5   # e.g. TechCrunch alone


# ---------------------------------------------------------------------------
# Tier 5 — known unreliable domains (reject immediately)
# ---------------------------------------------------------------------------

TIER5_DOMAINS: set[str] = {
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
    "activistpost.com",
    "21stcenturywire.com",
    "dcclothesline.com",
    "realnewsrightnow.com",
    "neonnettle.com",
    "thelastlineofdefense.org",
    "huzlers.com",
    "thelapine.ca",
}

# ---------------------------------------------------------------------------
# Tier 1 — Wire services & global broadcasters
# ---------------------------------------------------------------------------

TIER1_DOMAINS: set[str] = {
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "afp.com",
    "bloomberg.com",
    "aljazeera.com",
    "npr.org",
    "france24.com",
    "dw.com",
    "rfi.fr",
    "voanews.com",
    # Primary official evidence sources. An official statement counts as
    # authoritative evidence for what the organisation said, not as independent
    # confirmation that every claim in the statement is true.
    "un.org",
    "who.int",
    "europa.eu",
    "nato.int",
    "worldbank.org",
    "imf.org",
}

TIER1_NAME_FRAGMENTS: set[str] = {
    "reuters", "associated press", "ap news", " ap ",
    "bbc", "bloomberg", "al jazeera", "aljazeera", "afp",
    "agence france", "france 24", "npr", "voice of america",
    "deutsche welle", "radio france",
}

# ---------------------------------------------------------------------------
# Tier 2 — Established national/international newspapers & broadcasters
# ---------------------------------------------------------------------------

TIER2_DOMAINS: set[str] = {
    "nytimes.com",
    "washingtonpost.com",
    "theguardian.com",
    "ft.com",
    "economist.com",
    "cnn.com",
    "nbcnews.com",
    "abcnews.go.com",
    "cbsnews.com",
    "skynews.com",
    "time.com",
    "newsweek.com",
    "usatoday.com",
    "wsj.com",
    "latimes.com",
    "telegraph.co.uk",
    "independent.co.uk",
    "thetimes.co.uk",
    "lemonde.fr",
    "spiegel.de",
    "corriere.it",
    "elpais.com",
    "globo.com",
    "thehindu.com",
    "dawn.com",
    "ndtv.com",
    "timesofindia.indiatimes.com",
    "thenews.com.pk",
    "geo.tv",
    "arynews.tv",
    "tribune.com.pk",
    "pakistantoday.com.pk",
    "app.com.pk",
    "indianexpress.com",
    "hindustantimes.com",
    "theprint.in",
    "scroll.in",
    "thedailystar.net",
    "kathmandupost.com",
    "dailymirror.lk",
    "thenationalnews.com",
    "gulfnews.com",
    "timesofisrael.com",
    "dailystar.com.lb",
    "hurriyetdailynews.com",
    "euronews.com",
    "smh.com.au",
    "theage.com.au",
    "japantimes.co.jp",
    "koreaherald.com",
    "scmp.com",
    "straitstimes.com",
    "channelnewsasia.com",
    "rappler.com",
    "news24.com",
    "nation.africa",
    "premiumtimesng.com",
    "ahram.org.eg",
    "abc.net.au",
    "cbc.ca",
    "ctvnews.ca",
    "globalnews.ca",
    "mercopress.com",
    "buenosairesherald.com",
    "ebc.com.br",
    "rnz.co.nz",
    "sbs.com.au",
    "irishtimes.com",
    "rte.ie",
    "arabnews.com",
    "dailysabah.com",
    "africanews.com",
}

TIER2_NAME_FRAGMENTS: set[str] = {
    "new york times", "washington post", "the guardian", "financial times",
    "the economist", "cnn", "nbc news", "abc news", "cbs news", "sky news",
    "time magazine", "newsweek", "usa today", "wall street journal",
    "los angeles times", "the telegraph", "the independent", "le monde",
    "der spiegel", "the hindu", "dawn", "south china morning post",
    "straits times", "channel news asia", "irish times",
    "ndtv", "times of india", "geo news", "geo tv", "ary news",
    "the news", "tribune pakistan", "hindustan times", "the print",
    "indian express", "daily star", "kathmandu post", "the national",
    "gulf news", "times of israel", "hurriyet daily news", "euronews",
    "japan times", "korea herald", "rappler", "news24", "nation africa",
    "premium times", "ctv news", "global news", "mercopress",
    "buenos aires herald", "radio new zealand", "sbs news",
}

# ---------------------------------------------------------------------------
# Tier 3 — Specialist / trade publications
# ---------------------------------------------------------------------------

TIER3_DOMAINS: set[str] = {
    # Tech
    "techcrunch.com",
    "wired.com",
    "theverge.com",
    "arstechnica.com",
    "venturebeat.com",
    "zdnet.com",
    "engadget.com",
    "thenextweb.com",
    "9to5google.com",
    "macrumors.com",
    "techradar.com",
    "cnet.com",
    "gizmodo.com",
    # Politics
    "politico.com",
    "axios.com",
    "thehill.com",
    "rollcall.com",
    # Science
    "nature.com",
    "newscientist.com",
    "science.org",
    "scientificamerican.com",
    "sciencedaily.com",
    "livescience.com",
    "phys.org",
    # Business
    "cnbc.com",
    "forbes.com",
    "fortune.com",
    "marketwatch.com",
    "businessinsider.com",
    "hbr.org",
    "fastcompany.com",
    "inc.com",
    # Sports
    "espn.com",
    "skysports.com",
    "sportingnews.com",
    "bleacherreport.com",
    "cbssports.com",
    # Entertainment
    "variety.com",
    "hollywoodreporter.com",
    "deadline.com",
    "rollingstone.com",
    "billboard.com",
    "pitchfork.com",
    # Other specialist
    "foreignpolicy.com",
    "foreignaffairs.com",
    "theatlantic.com",
    "newyorker.com",
    "vox.com",
    "slate.com",
    "propublica.org",
    "intercept.com",
}

TIER3_NAME_FRAGMENTS: set[str] = {
    "techcrunch", "wired", "the verge", "ars technica", "politico", "axios",
    "the hill", "nature", "new scientist", "scientific american", "cnbc",
    "forbes", "fortune", "espn", "sky sports", "variety", "hollywood reporter",
    "deadline", "rolling stone", "the atlantic", "new yorker", "vox",
    "foreign policy", "pro publica",
}

# ---------------------------------------------------------------------------
# Tier 4 — Blogs, aggregators, tabloids (partial list — everything else = T4)
# ---------------------------------------------------------------------------

TIER4_DOMAINS: set[str] = {
    "buzzfeed.com",
    "buzzfeednews.com",
    "tmz.com",
    "people.com",
    "ew.com",
    "huffpost.com",
    "huffingtonpost.com",
    "dailymail.co.uk",
    "thesun.co.uk",
    "nypost.com",
    "pagesix.com",
    "dailybeast.com",
    "salon.com",
    "rawstory.com",
    "mediaite.com",
    "boingboing.net",
    "mentalfloss.com",
    "mashable.com",
    "reddit.com",
    "medium.com",
    "substack.com",
    "wordpress.com",
    "blogspot.com",
    "tumblr.com",
}

TIER4_NAME_FRAGMENTS: set[str] = {
    "buzzfeed", "tmz", "daily mail", "the sun", "new york post",
    "huffpost", "huffington post", "daily beast", "salon", "reddit",
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

@dataclass
class SourceClassification:
    domain:   str
    tier:     SourceTier
    weight:   float
    label:    str
    can_be_primary: bool


def classify_source(source_name: str, source_url: str) -> SourceClassification:
    """
    Classify a story's source into Tier 1–5.
    Uses domain first, then falls back to source name matching.
    Unknown sources default to Tier 4.
    """
    domain = canonical_domain(source_url)
    name   = source_name.lower()

    tier = _classify_by_domain(domain) or _classify_by_name(name) or SourceTier.TIER4

    return SourceClassification(
        domain         = domain,
        tier           = tier,
        weight         = tier.confidence_weight,
        label          = tier.label,
        can_be_primary = tier.can_be_primary,
    )


def compute_confidence(classifications: list[SourceClassification]) -> str:
    """
    Given a list of source classifications for a story cluster,
    compute the overall confidence level.

    Rules:
      - Any Tier 5 source in primary position → REJECT
      - Total weight ≥ CONFIDENCE_HIGH → HIGH
      - Total weight ≥ CONFIDENCE_GOOD → GOOD
      - Total weight ≥ CONFIDENCE_LOW  → LOW
      - Otherwise → REJECT
    """
    if not classifications:
        return Confidence.REJECT

    total_weight = sum(c.weight for c in classifications)

    # Must have at least one primary-eligible (Tier 1–3) source
    has_primary = any(c.can_be_primary for c in classifications)
    if not has_primary:
        return Confidence.REJECT

    if total_weight >= CONFIDENCE_HIGH:
        return Confidence.HIGH
    if total_weight >= CONFIDENCE_GOOD:
        return Confidence.GOOD
    if total_weight >= CONFIDENCE_LOW:
        return Confidence.LOW
    return Confidence.REJECT


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def canonical_domain(url: str) -> str:
    """Extract root domain from URL, stripping www. prefix."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host   = parsed.netloc or parsed.path
        host   = host.lower().strip()
        if host.startswith("www."):
            host = host[4:]
        # Strip port if present
        host = host.split(":")[0]
        all_known = (
            TIER5_DOMAINS | TIER1_DOMAINS | TIER2_DOMAINS
            | TIER3_DOMAINS | TIER4_DOMAINS
        )
        for known in sorted(all_known, key=len, reverse=True):
            if host == known or host.endswith(f".{known}"):
                return known
        return host
    except Exception:
        return ""


_PUBLISHER_GROUPS = {
    "bbc.com": "bbc",
    "bbc.co.uk": "bbc",
    "reuters.com": "reuters",
    "apnews.com": "associated-press",
    "associatedpress.com": "associated-press",
    "afp.com": "afp",
    "france24.com": "france24",
}


def publisher_identity(url_or_domain: str) -> str:
    """Return one ownership identity across a publisher's related domains."""
    domain = canonical_domain(url_or_domain)
    return _PUBLISHER_GROUPS.get(domain, domain)


# Backwards-compatible private alias used by older imports/tests.
_extract_domain = canonical_domain


def _classify_by_domain(domain: str) -> SourceTier | None:
    if not domain:
        return None
    # Check exact match first, then subdomain match
    for tier, domains in [
        (SourceTier.TIER5, TIER5_DOMAINS),
        (SourceTier.TIER1, TIER1_DOMAINS),
        (SourceTier.TIER2, TIER2_DOMAINS),
        (SourceTier.TIER3, TIER3_DOMAINS),
        (SourceTier.TIER4, TIER4_DOMAINS),
    ]:
        if domain in domains:
            return tier
        # subdomain match: feeds.reuters.com → reuters.com
        for known in domains:
            if domain.endswith(f".{known}"):
                return tier
    return None


def _classify_by_name(name: str) -> SourceTier | None:
    for tier, fragments in [
        (SourceTier.TIER1, TIER1_NAME_FRAGMENTS),
        (SourceTier.TIER2, TIER2_NAME_FRAGMENTS),
        (SourceTier.TIER3, TIER3_NAME_FRAGMENTS),
        (SourceTier.TIER4, TIER4_NAME_FRAGMENTS),
    ]:
        if any(frag in name for frag in fragments):
            return tier
    return None
