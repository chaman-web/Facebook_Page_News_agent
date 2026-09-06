"""
pipeline/do_not_publish.py — Pre-queue editorial gate for Global Pulse News.

This gate runs BEFORE a story enters the posting queue.
Every check returns a DNPVerdict with a decision (PUBLISH / HOLD / REJECT)
and a reason. Nothing passes silently — every story gets a logged decision.

Checks (in order):

  1.  DUPLICATE URL            → REJECT   (already published this exact story)
  2.  SIMILAR TITLE            → HOLD     (substantially same story, different source)
  3.  TOO OLD                  → REJECT   (story exceeds NEWS_MAX_AGE_HOURS)
  4.  WEAK SOURCE              → HOLD     (unverified domain, needs corroboration)
  5.  UNRELIABLE SOURCE        → REJECT   (known misinformation domain)
  6.  MISLEADING HEADLINE      → REJECT   (clickbait, sensationalism, fabrication signals)
  7.  NO MEANINGFUL CONTENT    → REJECT   (empty title/summary, removed article)
  8.  ENGAGEMENT BAIT          → REJECT   (trending only because of bait, not news value)
  9.  LOW NEWS VALUE           → REJECT   (score < minimum threshold)
  10. IMAGE MISMATCH           → REJECT   (image provided but topic clearly doesn't match)
  11. NO DEVELOPMENT           → REJECT   (story is a recap/roundup with no new facts)
  12. HEADLINE-BODY MISMATCH   → HOLD     (title makes claims not supported by summary)
  13. STORY VELOCITY           → HOLD     (too fresh, zero corroboration — unverified breaking)
  14. WEAK ATTRIBUTION         → HOLD     (entire story built on anonymous/unverified claims)

Decision hierarchy:
  REJECT > HOLD > PUBLISH
  If any check returns REJECT → story is rejected outright.
  If any check returns HOLD   → story is held (not queued) until next run.
  PUBLISH means all checks passed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

import config
from models import Story

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

class DNPDecision(str, Enum):
    PUBLISH = "PUBLISH"
    HOLD    = "HOLD"
    REJECT  = "REJECT"


@dataclass
class DNPVerdict:
    decision:   DNPDecision
    check_name: str
    reason:     str

    def __bool__(self) -> bool:
        return self.decision == DNPDecision.PUBLISH


# ---------------------------------------------------------------------------
# Recently published titles — in-memory ring buffer (last 50)
# Used for check #2 (similar title to recently published story)
# ---------------------------------------------------------------------------
_RECENT_PUBLISHED_TITLES: list[str] = []
_MAX_RECENT_TITLES = 50
_SIMILAR_TITLE_THRESHOLD = 0.72   # 72% similarity = substantially same story


def record_published_title(title: str) -> None:
    """Call this after every successful publish to feed check #2."""
    _RECENT_PUBLISHED_TITLES.append(title.lower().strip())
    if len(_RECENT_PUBLISHED_TITLES) > _MAX_RECENT_TITLES:
        _RECENT_PUBLISHED_TITLES.pop(0)


# ---------------------------------------------------------------------------
# Known unreliable domains
# ---------------------------------------------------------------------------

_UNRELIABLE_DOMAINS: set[str] = {
    "beforeitsnews.com", "yournewswire.com", "newspunch.com",
    "naturalnews.com", "infowars.com", "globalresearch.ca",
    "zerohedge.com", "thegatewaypundit.com", "worldnewsdailyreport.com",
    "empirenews.net", "anonhq.com", "collective-evolution.com",
    "activistpost.com", "21stcenturywire.com", "veteranstoday.com",
    "whatdoesitmean.com", "thenewsnerd.com", "newslo.com",
    "politicops.com", "abcnews.com.co",
}

# Weak source domains — hold until corroborated
_WEAK_SOURCE_DOMAINS: set[str] = {
    "reddit.com", "buzzfeed.com", "tmz.com", "nypost.com",
    "dailymail.co.uk", "thesun.co.uk", "mirror.co.uk",
    "express.co.uk", "nationalenquirer.com", "theonion.com",
    "clickhole.com",
}

# ---------------------------------------------------------------------------
# Misleading headline patterns
# ---------------------------------------------------------------------------

_MISLEADING_PATTERNS = [
    # Classic clickbait structures
    r"\bwon'?t believe\b",
    r"\byou (need to|must|have to) (see|know|watch|read)\b",
    r"\bshock(s|ed|ing)?\b.{0,20}\b(world|everyone|internet|nation)\b",
    r"\bbreaks? the internet\b",
    r"\bpeople are (losing|going) (it|crazy|mad|nuts)\b",
    r"\bthis is (why|what|how)\b.{0,30}\?$",
    r"\b\d+ (things|reasons|ways|tips|tricks|secrets|facts)\b",
    r"\bwhat happens next will\b",
    r"\b(doctors?|experts?|scientists?) (hate|don'?t want you to know)\b",
    # Fabricated/unverified claim signals
    r"\bsources? (say|claim|allege|report) exclusively\b",
    r"\bwe can (reveal|exclusively reveal)\b",
    r"\bclaims? to have (proof|evidence|video)\b",
    # Exaggerated superlatives that are rarely true
    r"\bmost (shocking|disturbing|disgusting|outrageous) (ever|in history)\b",
    r"\bworst (thing|event|disaster|decision) (ever|in history|of all time)\b",
    r"\bchanges everything\b",
    r"\bnothing will ever be the same\b",
    # Question headlines that imply false equivalence
    r"\bdid .{5,40} (cause|start|plan|do) .{5,40}\?$",
    r"\bis .{5,40} (really|actually|secretly) .{5,40}\?$",
]

# ---------------------------------------------------------------------------
# Engagement bait signals
# These stories trend purely because of bait — not news value
# ---------------------------------------------------------------------------

_ENGAGEMENT_BAIT_PATTERNS = [
    r"\bshare (this|if you)\b",
    r"\btag (a friend|someone)\b",
    r"\bcomment (below|your)\b.{0,20}\b(opinion|thoughts?|favourite)\b",
    r"\blike if you\b",
    r"\bprove .{0,20}% of people\b",
    r"\bonly \d+% (can|of people)\b",
    r"\bguess (the|which|who|what)\b",
    r"\bcan you (spot|find|see|guess)\b",
    r"\b(hot|fire|100|💯|🔥|😱|🤯) emoji(s)? (in the comments?|below)\b",
    r"\bquiz:\b",
    r"\bpoll:\b",
    r"\bvote:\b",
    r"\bwhat (type|kind) of .{3,30} are you\b",
]

# ---------------------------------------------------------------------------
# No-development / recap signals
# ---------------------------------------------------------------------------

_NO_DEVELOPMENT_PATTERNS = [
    r"^(recap|roundup|summary|week in review|month in review|year in review)[:\s]",
    r"\bweek(ly)? (recap|roundup|digest|newsletter)\b",
    r"\beverything (we know|that happened)\b",
    r"\ba look back\b",
    r"\bthen and now\b",
    r"\bthe (full|complete|entire) (story|timeline|history)\b",
    r"\bhow (it|this) (started|began|all began)\b",
    r"\b(explainer|primer):\b",
    r"\bwhat you (missed|need to know about)\b",
]

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def check_do_not_publish(
    story: Story,
    editorial_score: float | None = None,
    image_query: str | None = None,
) -> DNPVerdict:
    """
    Run all DNP checks on a story.
    Returns a DNPVerdict — PUBLISH means all checks passed.

    Args:
        story:           The story to check.
        editorial_score: Pre-computed editorial score (for check #9).
        image_query:     The Pexels search query used for the image (for check #10).
    """
    checks = [
        _check_no_meaningful_content,
        _check_too_old,
        _check_unreliable_source,
        _check_duplicate_url,
        _check_similar_title,
        _check_weak_source,
        _check_misleading_headline,
        _check_engagement_bait,
        _check_no_development,
        _check_story_velocity,         # NEW #13
        _check_weak_attribution,       # NEW #14
        _check_headline_body_mismatch, # NEW #12
        lambda s: _check_low_news_value(s, editorial_score),
        lambda s: _check_image_mismatch(s, image_query),
    ]

    verdicts: list[DNPVerdict] = []
    for check_fn in checks:
        verdict = check_fn(story)
        if verdict.decision != DNPDecision.PUBLISH:
            verdicts.append(verdict)

    # If any REJECT → final decision is REJECT (use first one)
    rejects = [v for v in verdicts if v.decision == DNPDecision.REJECT]
    if rejects:
        v = rejects[0]
        logger.warning("⛔ DNP REJECT [%s]: %s | Story: %s", v.check_name, v.reason, story.title[:60])
        return v

    # If any HOLD → final decision is HOLD
    holds = [v for v in verdicts if v.decision == DNPDecision.HOLD]
    if holds:
        v = holds[0]
        logger.info("🟡 DNP HOLD [%s]: %s | Story: %s", v.check_name, v.reason, story.title[:60])
        return v

    logger.debug("✅ DNP passed: %s", story.title[:60])
    return DNPVerdict(DNPDecision.PUBLISH, "ALL_CHECKS", "All DNP checks passed.")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_no_meaningful_content(story: Story) -> DNPVerdict:
    """Check #7 — title missing, removed, or summary is empty."""
    title = (story.title or "").strip()

    if not title or len(title) < 10:
        return DNPVerdict(DNPDecision.REJECT, "NO_CONTENT", "Title is missing or too short.")

    if "[Removed]" in title or "[Deleted]" in title:
        return DNPVerdict(DNPDecision.REJECT, "NO_CONTENT", "Article has been removed by source.")

    if not story.source_url or "http" not in story.source_url:
        return DNPVerdict(DNPDecision.REJECT, "NO_CONTENT", "Missing or invalid source URL.")

    return DNPVerdict(DNPDecision.PUBLISH, "NO_CONTENT", "")


def _check_too_old(story: Story) -> DNPVerdict:
    """Check #3 — story exceeds NEWS_MAX_AGE_HOURS."""
    now = datetime.now(timezone.utc)
    pub = story.published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    age_h = (now - pub).total_seconds() / 3600

    if age_h > config.NEWS_MAX_AGE_HOURS:
        return DNPVerdict(
            DNPDecision.REJECT, "TOO_OLD",
            f"Story is {age_h:.1f}h old (max {config.NEWS_MAX_AGE_HOURS}h).",
        )
    return DNPVerdict(DNPDecision.PUBLISH, "TOO_OLD", "")


def _check_unreliable_source(story: Story) -> DNPVerdict:
    """Check #5 — known misinformation / unreliable domain."""
    domain = _domain(story.source_url)
    if domain in _UNRELIABLE_DOMAINS:
        return DNPVerdict(
            DNPDecision.REJECT, "UNRELIABLE_SOURCE",
            f"Domain '{domain}' is on the unreliable source blocklist.",
        )
    return DNPVerdict(DNPDecision.PUBLISH, "UNRELIABLE_SOURCE", "")


def _check_duplicate_url(story: Story) -> DNPVerdict:
    """Check #1 — exact URL already published."""
    from pipeline.deduplicator import _load_seen
    seen = _load_seen()
    if story.source_url in seen.get("urls", []):
        return DNPVerdict(
            DNPDecision.REJECT, "DUPLICATE_URL",
            f"URL already published: {story.source_url}",
        )
    return DNPVerdict(DNPDecision.PUBLISH, "DUPLICATE_URL", "")


def _check_similar_title(story: Story) -> DNPVerdict:
    """Check #2 — substantially similar to a recently published story."""
    norm = _normalise(story.title)
    for recent in _RECENT_PUBLISHED_TITLES:
        ratio = SequenceMatcher(None, norm, recent).ratio()
        if ratio >= _SIMILAR_TITLE_THRESHOLD:
            return DNPVerdict(
                DNPDecision.HOLD, "SIMILAR_TITLE",
                f"Title is {ratio:.0%} similar to recently published story.",
            )
    return DNPVerdict(DNPDecision.PUBLISH, "SIMILAR_TITLE", "")


def _check_weak_source(story: Story) -> DNPVerdict:
    """Check #4 — weak/tabloid source with no corroboration → HOLD."""
    domain = _domain(story.source_url)
    if domain in _WEAK_SOURCE_DOMAINS:
        # If corroborated by another source, allow it
        if story.corroborating_sources:
            return DNPVerdict(DNPDecision.PUBLISH, "WEAK_SOURCE", "")
        return DNPVerdict(
            DNPDecision.HOLD, "WEAK_SOURCE",
            f"Weak/tabloid source '{domain}' with no corroboration. Holding until verified.",
        )
    return DNPVerdict(DNPDecision.PUBLISH, "WEAK_SOURCE", "")


def _check_misleading_headline(story: Story) -> DNPVerdict:
    """Check #6 — clickbait, sensationalist, or fabrication signals in title."""
    title = story.title or ""
    for pattern in _MISLEADING_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            return DNPVerdict(
                DNPDecision.REJECT, "MISLEADING_HEADLINE",
                f"Headline matches misleading pattern: '{pattern}'",
            )
    return DNPVerdict(DNPDecision.PUBLISH, "MISLEADING_HEADLINE", "")


def _check_engagement_bait(story: Story) -> DNPVerdict:
    """Check #8 — story is only trending because of engagement bait."""
    text = (story.title + " " + (story.raw_summary or "")).lower()
    for pattern in _ENGAGEMENT_BAIT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return DNPVerdict(
                DNPDecision.REJECT, "ENGAGEMENT_BAIT",
                f"Story contains engagement bait pattern: '{pattern}'",
            )
    return DNPVerdict(DNPDecision.PUBLISH, "ENGAGEMENT_BAIT", "")


def _check_no_development(story: Story) -> DNPVerdict:
    """Check #11 — story is a recap/roundup/explainer with no new facts."""
    title = (story.title or "").lower()
    for pattern in _NO_DEVELOPMENT_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            return DNPVerdict(
                DNPDecision.REJECT, "NO_DEVELOPMENT",
                f"Story appears to be a recap/roundup with no new development.",
            )
    return DNPVerdict(DNPDecision.PUBLISH, "NO_DEVELOPMENT", "")


def _check_low_news_value(story: Story, score: float | None) -> DNPVerdict:
    """Check #9 — editorial score too low to publish."""
    if score is None:
        return DNPVerdict(DNPDecision.PUBLISH, "LOW_NEWS_VALUE", "")
    if score < 60.0:
        return DNPVerdict(
            DNPDecision.REJECT, "LOW_NEWS_VALUE",
            f"Editorial score {score:.1f} < minimum 60 required to publish.",
        )
    return DNPVerdict(DNPDecision.PUBLISH, "LOW_NEWS_VALUE", "")


def _check_image_mismatch(story: Story, image_query: str | None) -> DNPVerdict:
    """
    Check #10 — image search query clearly doesn't match the story topic.
    We check by comparing key nouns from the title against the query.
    This is a soft check — only rejects on obvious mismatch.
    """
    if not image_query:
        return DNPVerdict(DNPDecision.PUBLISH, "IMAGE_MISMATCH", "")

    title_words = set(re.findall(r"\b[a-z]{4,}\b", story.title.lower()))
    query_words = set(re.findall(r"\b[a-z]{4,}\b", image_query.lower()))

    # Remove very common words that appear everywhere
    stopwords = {"that", "with", "this", "from", "they", "their", "have", "been",
                 "will", "were", "after", "over", "into", "about", "more", "also"}
    title_words -= stopwords
    query_words -= stopwords

    if not title_words or not query_words:
        return DNPVerdict(DNPDecision.PUBLISH, "IMAGE_MISMATCH", "")

    # Overlap ratio
    overlap = len(title_words & query_words) / max(len(title_words), 1)

    if overlap < 0.10 and len(title_words) > 4:
        return DNPVerdict(
            DNPDecision.REJECT, "IMAGE_MISMATCH",
            f"Image query '{image_query[:40]}' has <10% overlap with story title. "
            "Image likely does not match story.",
        )
    return DNPVerdict(DNPDecision.PUBLISH, "IMAGE_MISMATCH", "")


# ---------------------------------------------------------------------------
# Check #12 — Headline-body consistency
#
# Numbers / magnitudes claimed in the title must appear in the summary too.
# "100 killed" in the title but the body says "casualties" → mismatch.
# Also flags strong verbs (confirms, signs, launches) with no matching body signal.
# ---------------------------------------------------------------------------

# Strong claim words that MUST have a corresponding signal in the summary
_HEADLINE_CLAIM_VERBS = [
    "confirm", "sign", "launch", "announce", "declare", "arrest",
    "kill", "die", "dies", "dead", "attack", "strike", "bomb",
    "invade", "resign", "fire", "ban", "sanction", "condemn",
]

# ---------------------------------------------------------------------------
# Check #13 — Story velocity (too fresh, no corroboration)
#
# A story published < VELOCITY_MIN_AGE_MINUTES ago with ZERO corroborating
# sources is unverified breaking news. Hold it — let it age 15 minutes.
# If it's real, corroboration will appear quickly.
# Exception: stories from Tier 1 trusted sources bypass this.
# ---------------------------------------------------------------------------

VELOCITY_MIN_AGE_MINUTES = 15   # stories younger than this need corroboration
VELOCITY_HOLD_HOURS      = 1    # how long to hold before retry (informational)

_TRUSTED_FAST_SOURCES = {
    # These sources are trusted enough to publish immediately without corroboration
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
    "theguardian.com", "nytimes.com", "washingtonpost.com",
    "ft.com", "bloomberg.com", "wsj.com", "economist.com",
    "aljazeera.com", "dw.com", "france24.com", "abc.net.au",
    "cnn.com", "nbcnews.com", "cbsnews.com", "abcnews.go.com",
    "politico.com", "thehill.com", "axios.com", "npr.org",
    "time.com", "foreignpolicy.com", "cfr.org",
}

# ---------------------------------------------------------------------------
# Check #14 — Weak attribution
#
# Stories built entirely on anonymous/unverified claims should be held.
# These phrases signal weak attribution in the summary text.
# Strong attribution phrases override them.
# ---------------------------------------------------------------------------

_WEAK_ATTRIBUTION_PHRASES = [
    r"\bsources? (familiar with|close to|with knowledge of|told\b)",
    r"\baccording to (anonymous|unnamed|undisclosed|unidentified) sources?\b",
    r"\bwho (spoke|asked) (on|for) (the )?condition of anonymity\b",
    r"\bsources? say\b",
    r"\bunconfirmed (report|claim|source)\b",
    r"\ballegedly\b",
    r"\bhas not (confirmed|verified|responded)\b",
    r"\bcould not (independently )?verify\b",
    r"\bsaid to (have|be)\b",
    r"\bpurportedly\b",
    r"\bapparently\b",
    r"\bclaims? to (have|show|prove)\b",
]

# If the summary contains these, attribution is strong → override weak signals
_STRONG_ATTRIBUTION_PHRASES = [
    r"\b(confirmed|announced|said|stated) (by|in) (a )?(statement|press conference|briefing)\b",
    r"\bofficial (statement|announcement|confirmation)\b",
    r"\bspokesperson (said|confirmed|told)\b",
    r"\bgovernment (said|confirmed|announced)\b",
    r"\b(reuters|bbc|ap|associated press|afp) (report(s|ed)?|confirm(s|ed)?)\b",
    r"\baccording to (reuters|bbc|ap|afp|the guardian|nytimes?)\b",
    r"\bhas confirmed\b",
    r"\bofficially (confirmed|announced|declared)\b",
]

# Only trigger weak-attribution hold if this many weak phrases are found
_WEAK_ATTRIBUTION_THRESHOLD = 2


# ---------------------------------------------------------------------------
# Check #12 — Headline-body consistency
# ---------------------------------------------------------------------------

def _check_headline_body_mismatch(story: Story) -> DNPVerdict:
    """
    Headline makes claims the summary doesn't support.

    Two sub-checks:
      a) Numeric mismatch: a large number in the title doesn't appear in summary.
      b) Strong verb mismatch: headline uses confirm/arrest/bomb etc but
         no matching word appears in the summary.
    """
    title   = story.title or ""
    summary = (story.raw_summary or "").strip()

    if not summary or len(summary) < 30:
        return DNPVerdict(DNPDecision.PUBLISH, "HEADLINE_BODY_MISMATCH", "")

    # --- a) Numeric mismatch ---
    title_nums   = set(re.findall(r"\b\d[\d,]*\b", title))
    summary_nums = set(re.findall(r"\b\d[\d,]*\b", summary))
    significant  = {n for n in title_nums if int(n.replace(",", "")) >= 10}
    if significant and not (significant & summary_nums):
        return DNPVerdict(
            DNPDecision.HOLD, "HEADLINE_BODY_MISMATCH",
            f"Title claims number(s) {significant} not found in summary — "
            "possible exaggeration or outdated figure.",
        )

    # --- b) Strong verb without summary support ---
    for verb in _HEADLINE_CLAIM_VERBS:
        if re.search(rf"\b{verb}(s|ed|ing)?\b", title, re.IGNORECASE):
            if not re.search(rf"\b{verb}(s|ed|ing|ation|ment)?\b", summary, re.IGNORECASE):
                return DNPVerdict(
                    DNPDecision.HOLD, "HEADLINE_BODY_MISMATCH",
                    f"Title uses strong verb '{verb}' but summary has no supporting text. "
                    "Headline may be stronger than the story.",
                )

    return DNPVerdict(DNPDecision.PUBLISH, "HEADLINE_BODY_MISMATCH", "")


# ---------------------------------------------------------------------------
# Check #13 — Story velocity (too fresh, no corroboration)
# ---------------------------------------------------------------------------

def _check_story_velocity(story: Story) -> DNPVerdict:
    """
    Story is < 15 minutes old with no corroboration from a non-trusted source.
    Very new single-source stories are high-risk — hold until they age or
    corroboration appears.
    """
    domain = _domain(story.source_url)
    if domain in _TRUSTED_FAST_SOURCES:
        return DNPVerdict(DNPDecision.PUBLISH, "STORY_VELOCITY", "")

    pub = story.published_at
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - pub).total_seconds() / 60

    if age_minutes < VELOCITY_MIN_AGE_MINUTES:
        corroborated = bool(getattr(story, "corroborating_sources", None))
        if not corroborated:
            return DNPVerdict(
                DNPDecision.HOLD, "STORY_VELOCITY",
                f"Story is only {age_minutes:.0f}m old with no corroboration "
                f"from '{domain}'. Holding until it ages or is verified.",
            )

    return DNPVerdict(DNPDecision.PUBLISH, "STORY_VELOCITY", "")


# ---------------------------------------------------------------------------
# Check #14 — Weak attribution
# ---------------------------------------------------------------------------

def _check_weak_attribution(story: Story) -> DNPVerdict:
    """
    Story is built entirely on anonymous or unverified claims.
    Strong attribution (official statement, named agency confirmation) overrides.
    """
    summary = (story.raw_summary or "").strip()
    if not summary or len(summary) < 40:
        return DNPVerdict(DNPDecision.PUBLISH, "WEAK_ATTRIBUTION", "")

    # Strong attribution overrides everything
    for phrase in _STRONG_ATTRIBUTION_PHRASES:
        if re.search(phrase, summary, re.IGNORECASE):
            return DNPVerdict(DNPDecision.PUBLISH, "WEAK_ATTRIBUTION", "")

    # Count weak signals
    weak_count = sum(
        1 for phrase in _WEAK_ATTRIBUTION_PHRASES
        if re.search(phrase, summary, re.IGNORECASE)
    )

    if weak_count >= _WEAK_ATTRIBUTION_THRESHOLD:
        return DNPVerdict(
            DNPDecision.HOLD, "WEAK_ATTRIBUTION",
            f"Summary has {weak_count} weak-attribution signal(s) "
            "(anonymous sources, unconfirmed claims) with no strong attribution. "
            "Holding until corroborated.",
        )

    return DNPVerdict(DNPDecision.PUBLISH, "WEAK_ATTRIBUTION", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
        return ""


def _normalise(text: str) -> str:
    return "".join(c.lower() for c in text if c.isalnum() or c.isspace()).strip()
