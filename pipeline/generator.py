"""
pipeline/generator.py — Generate an original Facebook post using a local Ollama LLM.

Default backend: Ollama (local, free, no API key needed).
Fallback backend: OpenAI (if OPENAI_API_KEY is set and LLM_BACKEND=openai in .env).

Ollama exposes an OpenAI-compatible REST API at http://localhost:11434/v1,
so we use the openai SDK pointed at localhost — no extra library needed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import config
from models import GenerationError, Story, VerificationStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def _get_client():
    """Return an openai.OpenAI client pointed at the correct backend."""
    from openai import OpenAI

    if config.LLM_BACKEND == "ollama":
        return OpenAI(
            base_url=config.OLLAMA_BASE_URL,
            api_key="ollama",  # Ollama ignores this value but the SDK requires it
        )
    else:
        return OpenAI(api_key=config.OPENAI_API_KEY)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def generate_post(story: Story) -> Story:
    """
    Generate an original Facebook post from the verified story facts.
    Sets story.post_content and story.hashtags.
    Raises GenerationError on failure.
    Retries up to MAX_GENERATION_RETRIES times if hashtags are missing.
    """
def generate_post(story: Story) -> Story:
    """
    Generate an original Facebook post from the verified story facts.
    Sets story.post_content and story.hashtags.
    Raises GenerationError only if Ollama returns truly nothing (completely empty).

    Philosophy — fix it, don't reject it:
    - Enrich thin summaries before sending to Ollama
    - Accept short posts for genuinely short stories
    - Only retry if Ollama returned nothing at all
    - Hashtags always generated deterministically
    """
    from openai import OpenAIError

    MAX_GENERATION_RETRIES = 3
    model = config.OLLAMA_MODEL if config.LLM_BACKEND == "ollama" else config.OPENAI_MODEL

    logger.info(
        "Generating post with %s model '%s' for: %s",
        config.LLM_BACKEND.upper(),
        model,
        story.title,
    )

    # Step 1: Enrich thin story context before sending to Ollama
    story = _enrich_story_context(story)

    summary_len = len(story.raw_summary or "")
    # Absolute minimum we'll accept — purely empty responses get retried
    # Short stories (thin summary) → accept 60+ chars
    # Medium stories → accept 80+ chars
    # Rich stories → accept 150+ chars, but don't reject if Ollama gives less
    hard_min = 60  # anything under this is treated as "Ollama returned nothing"

    last_error: Exception | None = None
    best_output: str = ""  # track best attempt so far

    for attempt in range(1, MAX_GENERATION_RETRIES + 1):
        if attempt > 1:
            logger.info("Retrying generation (attempt %d/%d) — previous output too short.", attempt, MAX_GENERATION_RETRIES)

        prompt = _build_prompt(story)

        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
            )
        except OpenAIError as exc:
            raise GenerationError(f"LLM call failed ({config.LLM_BACKEND}): {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise GenerationError(
                f"LLM call failed ({config.LLM_BACKEND}): {exc}\n"
                "If using Ollama, make sure it is running: ollama serve"
            ) from exc

        raw_output = response.choices[0].message.content or ""
        logger.debug("LLM raw output:\n%s", raw_output)

        post_content = _parse_post_body(raw_output)

        if not post_content or len(post_content) < hard_min:
            # Keep the best we've seen so far (longest non-empty output)
            if post_content and len(post_content) > len(best_output):
                best_output = post_content
            last_error = GenerationError(f"LLM returned too little content ({len(post_content)} chars).")
            continue

        # Good enough — accept it regardless of length
        if summary_len > 200 and len(post_content) < 150:
            logger.warning(
                "Post is shorter than ideal for a rich story (%d chars) — publishing anyway.",
                len(post_content),
            )

        hashtags     = _generate_hashtags(story)
        post_content = _format_post(post_content, story)
        story.post_content  = post_content
        story.hashtags      = hashtags
        story.card_headline = (_parse_card_headline(raw_output)
                               or _fallback_card_headline(story))
        logger.info("Post generated (%d chars). Card: %s | Hashtags: %s",
                    len(post_content), story.card_headline, " ".join(hashtags))
        return story

    # All retries returned nothing — use the best partial output if available
    if best_output and len(best_output) >= 40:
        logger.warning(
            "All %d attempts returned short content — using best partial output (%d chars).",
            MAX_GENERATION_RETRIES, len(best_output),
        )
        hashtags = _generate_hashtags(story)
        story.post_content  = _format_post(best_output, story)
        story.hashtags      = hashtags
        story.card_headline = _fallback_card_headline(story)
        return story

    raise GenerationError(f"Post generation failed after {MAX_GENERATION_RETRIES} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Story context enrichment (Rule 2 — thin story handling)
# ---------------------------------------------------------------------------

def _enrich_story_context(story: Story) -> Story:
    """
    Before sending to Ollama, try to enrich a thin story with more content.
    Sources (in order):
      1. RSS full content field
      2. Article description
      3. Corroborating source summaries
    Never invents or pads — only uses verified source material.
    """
    summary = story.raw_summary or ""

    # Already rich enough
    if len(summary) >= 200:
        return story

    enriched_parts: list[str] = [summary]

    # Pull from corroborating sources if they carry extra text
    if story.corroborating_sources:
        for src in story.corroborating_sources:
            extra = src.get("description") or src.get("content") or src.get("summary") or ""
            if extra and extra not in summary:
                enriched_parts.append(extra)

    combined = " ".join(p.strip() for p in enriched_parts if p.strip())

    if len(combined) > len(summary):
        logger.info(
            "Story enriched: %d → %d chars for '%s'",
            len(summary), len(combined), story.title[:60],
        )
        story = story.__class__(
            **{**story.__dict__, "raw_summary": combined}
        )

    return story


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are the Facebook News Publishing Agent for Global Pulse News.

YOUR ROLE:
Write clear, factual, engaging Facebook posts based on verified news facts.
Prioritize CONTENT QUALITY, NEWS VALUE, ORIGINALITY and TIMING over posting volume.

CONTENT RULES:
- Never copy article text word-for-word. Write in your own words.
- Use plain language suitable for a general worldwide audience.
- Clearly label unconfirmed information with "reportedly", "according to sources", or "it is alleged".
- Do not sensationalise or use clickbait. Headlines must accurately represent the story.
- Do not include personal opinions or commentary.
- Never make misleading or exaggerated claims.
- The post must feel informative and trustworthy.
- NEVER invent details, fill gaps with assumptions, or repeat sentences to reach a word count.
- Write only what the provided facts support. If facts are thin, write a shorter post.

POST LENGTH — based on available information:
- Rich story (detailed summary + multiple sources): 200–400 words
- Medium story: 100–200 words
- Thin but valid story (brief announcement, ceremony, statement): 60–120 words
- Never pad to reach a minimum. A short honest post beats a padded fake one.

FORMATTING RULES (follow exactly):
- Write in SHORT PARAGRAPHS — maximum 2-3 sentences per paragraph.
- Put a BLANK LINE between every paragraph.
- DO NOT start with boring phrases like "In a significant development..." or "According to reports..." or "It has been reported that..."
- Use 1-2 relevant emojis per paragraph as visual anchors — not decoration.
- End with a direct QUESTION to the audience. This is mandatory.

FIRST LINE IS CRITICAL — THE HOOK:
Facebook shows only the first 2-3 lines before "See more". These lines must STOP THE SCROLL.
Line 1: A LABEL + the single most striking fact. Format: [LABEL] 🔴/📌/⚡ + ONE punchy sentence under 12 words.
         Examples:
         "🔴 BREAKING: Amazon cargo plane crashes at Miami airport — 5 dead."
         "⚡ JUST IN: Germany's far-right AfD wins landslide in eastern states."
         "📌 DEVELOPING: Delhi building collapse — 6 killed, dozens still trapped."
         "🌍 WORLD: Pakistan formally rejects India's claims over Kashmir."
Line 2: What it means OR why it matters — one sentence that gives context or stakes.
Line 3 (optional): One key detail or number that adds weight.

At the very end of the post, always include:
📰 Sources: <comma-separated source names>

Output format (return exactly this structure, nothing else):
CARD_HEADLINE:
<newspaper front-page splash — max 7 words, ALL CAPS, active voice. Must be a COMPLETE thought that stands alone. Never cut off mid-phrase. Use the most dramatic fact: numbers, death toll, country, key verb.

BAD (cut off): "75% OF A&E STAFF IN UK FACE"
GOOD: "75% OF NHS STAFF FACE DAILY VIOLENCE"

BAD (cut off): "GERMANY AFD WINS HISTORIC LANDSLIDE IN EASTERN"
GOOD: "AFD WINS GERMANY LANDSLIDE — FAR-RIGHT SURGES"

BAD (cut off): "DELHI BUILDING COLLAPSE KILLS 6, DOZENS STILL"
GOOD: "6 DEAD AS DELHI BUILDING COLLAPSES"

BAD (vague): "PAKISTAN REJECTS INDIA BASELESS CLAIMS ON OCCUPIED"
GOOD: "PAKISTAN REJECTS INDIA'S KASHMIR CLAIMS"

Write the headline as a punchy complete statement, not a truncated title.>

POST:
<your full Facebook post text here>
"""


# Category emoji map — injected into the prompt so LLM uses the right one
_CATEGORY_EMOJI = {
    "breaking":      "🔴",
    "technology":    "📱",
    "business":      "💼",
    "politics":      "🏛️",
    "science":       "🔬",
    "sports":        "🏆",
    "trending":      "🔥",
    "entertainment": "🎬",
    "jobs":          "💼",
    "world":         "🌍",
    "crime":         "🚨",
    "climate":       "🌿",
    "war":           "⚔️",
    "wellness":      "💪",
}


def _build_prompt(story: Story) -> str:
    """Build the user-facing prompt with structured story facts."""
    published = story.published_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build deduplicated source list: original + corroborating
    all_source_names = [story.source_name]
    if story.corroborating_sources:
        all_source_names += [s["name"] for s in story.corroborating_sources]
    seen_names: set[str] = set()
    unique_source_names: list[str] = []
    for name in all_source_names:
        if name not in seen_names:
            seen_names.add(name)
            unique_source_names.append(name)

    corroborating_text = ""
    if story.corroborating_sources:
        corroborating_text = (
            f"\nAdditional sources reporting this story: {', '.join(unique_source_names[1:])}"
        )

    verification_note = {
        VerificationStatus.VERIFIED: "This story has been confirmed by multiple reliable sources.",
        VerificationStatus.UNVERIFIED: (
            "This story is from a single source and has not been independently confirmed. "
            "Label any unconfirmed claims appropriately."
        ),
    }.get(story.verification_status, "")

    category    = getattr(story, "category", "breaking")
    cat_emoji   = _CATEGORY_EMOJI.get(category, "🌍")

    # Build a suggested hook label based on category and story freshness
    age_h = (datetime.now(timezone.utc) - story.published_at).total_seconds() / 3600
    if category == "breaking" or age_h < 2:
        hook_label = "🔴 BREAKING"
    elif category == "war":
        hook_label = "⚔️ WAR UPDATE"
    elif category == "politics":
        hook_label = "🏛️ POLITICS"
    elif category == "technology":
        hook_label = "📱 TECH"
    elif category == "business":
        hook_label = "💼 BUSINESS"
    elif category == "sports":
        hook_label = "🏆 SPORTS"
    elif category == "crime":
        hook_label = "🚨 CRIME"
    elif category == "climate":
        hook_label = "🌿 CLIMATE"
    elif category == "science":
        hook_label = "🔬 SCIENCE"
    elif category == "entertainment":
        hook_label = "🎬 ENTERTAINMENT"
    elif category == "wellness":
        hook_label = "💪 HEALTH"
    elif category == "jobs":
        hook_label = "💼 JOBS"
    elif age_h < 6:
        hook_label = "⚡ JUST IN"
    else:
        hook_label = "🌍 WORLD"

    return f"""Write a Facebook post about the following news story for Global Pulse News.

TITLE: {story.title}
SOURCE: {story.source_name}
PUBLISHED: {published}
SUMMARY: {story.raw_summary or "No summary available."}
VERIFICATION: {verification_note}{corroborating_text}
ALL SOURCES TO CITE: {', '.join(unique_source_names)}
CATEGORY EMOJI: {cat_emoji}

MANDATORY FIRST LINE — start the post with this exact label format:
"{hook_label}: [most striking fact from this story in under 12 words]"

Example for this story:
"{hook_label}: {story.title[:60]}{'...' if len(story.title) > 60 else ''}"

The post MUST follow this structure:
LINE 1: {hook_label}: [single most striking fact — under 12 words]
LINE 2: [what it means or why it matters — one sentence]
LINE 3 (optional): [one key detail or number that adds weight]
[BLANK LINE]
BODY: 2-3 short paragraphs (max 2-3 sentences each), blank line between each.
CLOSING QUESTION: Ask the audience something specific and thought-provoking. End with 👇
SOURCES: 📰 Sources: {', '.join(unique_source_names)}

Write the post now."""


# ---------------------------------------------------------------------------
# Post formatting — enforce short paragraphs + question ending
# ---------------------------------------------------------------------------

_QUESTION_FALLBACKS = [
    "What do you think about this? Share your thoughts below 👇",
    "How do you see this unfolding? Drop your opinion below 👇",
    "Do you think this will make a difference? Let us know below 👇",
    "What's your take on this? Comment below 👇",
]

def _format_post(post: str, story: Story | None = None) -> str:
    """
    1. Ensure the post starts with a label+hook line.
    2. Ensure blank lines between paragraphs (max 3 sentences per paragraph).
    3. Ensure the post ends with an engagement question.
    """
    import random

    # --- Enforce hook label on first line ---
    if story is not None:
        category = getattr(story, "category", "breaking")
        age_h    = (datetime.now(timezone.utc) - story.published_at).total_seconds() / 3600

        if category == "breaking" or age_h < 2:
            label = "🔴 BREAKING"
        elif category == "war":
            label = "⚔️ WAR UPDATE"
        elif category == "politics":
            label = "🏛️ POLITICS"
        elif category == "technology":
            label = "📱 TECH"
        elif category == "business":
            label = "💼 BUSINESS"
        elif category == "sports":
            label = "🏆 SPORTS"
        elif category == "crime":
            label = "🚨 CRIME"
        elif category == "climate":
            label = "🌿 CLIMATE"
        elif category == "science":
            label = "🔬 SCIENCE"
        elif category == "entertainment":
            label = "🎬 ENTERTAINMENT"
        elif category == "wellness":
            label = "💪 HEALTH"
        elif category == "jobs":
            label = "💼 JOBS"
        elif age_h < 6:
            label = "⚡ JUST IN"
        else:
            label = "🌍 WORLD"

        first_line = post.lstrip().split("\n")[0]
        label_patterns = ["🔴", "⚡", "📌", "🌍", "⚔️", "🏛️", "📱", "💼", "🏆",
                          "🚨", "🌿", "🔬", "🎬", "💪", "BREAKING", "JUST IN",
                          "WORLD", "DEVELOPING", "UPDATE"]
        has_label = any(p in first_line for p in label_patterns)

        if not has_label:
            # Build a short hook from the title
            title_words = story.title.split()
            short_hook  = " ".join(title_words[:10]) + ("..." if len(title_words) > 10 else "")
            hook_line   = f"{label}: {short_hook}"
            post        = hook_line + "\n\n" + post.lstrip()

    # --- Collapse excess blank lines first ---
    post = re.sub(r"\n{3,}", "\n\n", post.strip())

    # --- Split into paragraphs ---
    paragraphs = [p.strip() for p in post.split("\n\n") if p.strip()]

    split_paragraphs: list[str] = []
    for para in paragraphs:
        sentences = re.split(r"(?<=[.!?])\s+", para)
        chunk: list[str] = []
        for sentence in sentences:
            chunk.append(sentence)
            if len(chunk) >= 3:
                split_paragraphs.append(" ".join(chunk))
                chunk = []
        if chunk:
            split_paragraphs.append(" ".join(chunk))

    # --- Ensure closing question ---
    last = split_paragraphs[-1] if split_paragraphs else ""
    tail = " ".join(split_paragraphs[-2:]) if len(split_paragraphs) >= 2 else last
    has_question = "?" in tail or "👇" in tail
    if not has_question:
        split_paragraphs.append(random.choice(_QUESTION_FALLBACKS))

    # --- HOOK BLOCK: first 2 paragraphs joined with single \n (no blank line) ---
    # Facebook shows ~2-3 lines before "See more". A blank line eats one of those
    # visible lines. Pack the hook + context tight so both show before the cut.
    if len(split_paragraphs) >= 2:
        hook_block = split_paragraphs[0] + "\n" + split_paragraphs[1]
        body       = split_paragraphs[2:]
        return hook_block + ("\n\n" + "\n\n".join(body) if body else "")
    else:
        return "\n\n".join(split_paragraphs)

    # --- Ensure closing question ---
    last = split_paragraphs[-1] if split_paragraphs else ""
    tail = " ".join(split_paragraphs[-2:]) if len(split_paragraphs) >= 2 else last
    has_question = "?" in tail or "👇" in tail
    if not has_question:
        split_paragraphs.append(random.choice(_QUESTION_FALLBACKS))

    return "\n\n".join(split_paragraphs)


# ---------------------------------------------------------------------------
# Response parsing — post body only (hashtags generated separately)
# ---------------------------------------------------------------------------

def _fallback_card_headline(story: "Story") -> str:
    """
    Deterministic card headline from story.title when LLM doesn't produce one.
    - Strips source suffix ("- BBC News", "| Reuters", etc.)
    - Removes filler openers ("Watch:", "Reports:", weak quantifiers, etc.)
    - Compresses to max 7 words in ALL CAPS
    """
    title = story.title or ""
    # Strip source attribution tails
    for sep in [" - ", " | ", " — ", " – "]:
        if sep in title:
            title = title[:title.rfind(sep)].strip()
    # Strip leading filler labels
    title = re.sub(
        r"^(WATCH|REPORT|DEVELOPING|UPDATE|EXCLUSIVE|OPINION|ANALYSIS|VIDEO)\s*[:\-–]\s*",
        "", title, flags=re.IGNORECASE
    ).strip()
    # Strip weak quantity openers
    title = re.sub(
        r"^(ALMOST|NEARLY|ABOUT|ROUGHLY|OVER|MORE THAN|UP TO|AT LEAST)\s+",
        "", title, flags=re.IGNORECASE
    ).strip()
    # Trim to max 45 chars at word boundary, drop trailing weak words
    if len(title) > 45:
        title = title[:45].rsplit(" ", 1)[0]
    _weak_tail = {"IN", "ON", "AT", "OF", "TO", "THE", "A", "AN", "AND",
                  "OR", "BUT", "FOR", "WITH", "AS", "BY", "FROM", "STILL",
                  "INTO", "OVER", "AFTER", "AMID", "THAT", "THIS", "ITS"}
    words = title.split()
    while words and words[-1].upper().strip(".,") in _weak_tail:
        words.pop()
    return " ".join(words).upper() if words else title.upper()


def _parse_card_headline(raw: str) -> str | None:
    """Extract and clean the CARD_HEADLINE line from LLM output."""
    m = re.search(r"CARD_HEADLINE:\s*\n(.+)", raw, re.IGNORECASE)
    if not m:
        return None
    headline = m.group(1).strip().strip('"').strip("'")

    # Remove weak filler openers
    headline = re.sub(
        r"^(ALMOST|NEARLY|ABOUT|ROUGHLY|OVER|MORE THAN|UP TO|AT LEAST)\s+",
        "", headline, flags=re.IGNORECASE
    ).strip()

    # Trim to max 45 chars at word boundary
    if len(headline) > 45:
        headline = headline[:45].rsplit(" ", 1)[0]

    # Drop trailing weak words
    _weak_tail = {"IN", "ON", "AT", "OF", "TO", "THE", "A", "AN", "AND",
                  "OR", "BUT", "FOR", "WITH", "AS", "BY", "FROM", "STILL",
                  "INTO", "OVER", "AFTER", "AMID", "THAT", "THIS", "ITS",
                  "STATUS", "ISSUES", "MATTER", "SITUATION",
                  "MOVE", "STEP", "ACT", "SAYS", "SAID"}
    words = headline.split()
    while words and words[-1].upper().strip(".,") in _weak_tail:
        words.pop()

    return " ".join(words).upper() if words else None


def _parse_post_body(raw: str) -> str:
    """Extract only the post body from the LLM response. Ignores hashtag section."""
    # Try structured POST: section first
    post_match = re.search(r"POST:\s*\n(.*?)(?=\nHASHTAGS:|\Z)", raw, re.DOTALL | re.IGNORECASE)
    if post_match:
        return post_match.group(1).strip()
    # Fallback: strip any trailing hashtag lines and return the rest
    lines = raw.strip().splitlines()
    body_lines = [l for l in lines if not re.match(r"^\s*HASHTAGS:", l, re.IGNORECASE)]
    # Remove trailing lines that are only hashtags
    while body_lines and re.match(r"^\s*#\w+", body_lines[-1]):
        body_lines.pop()
    return "\n".join(body_lines).strip()


# ---------------------------------------------------------------------------
# Deterministic hashtag generation — always 3–5 relevant tags
# ---------------------------------------------------------------------------

# Category → base hashtags (always included for that category)
_CATEGORY_HASHTAGS: dict[str, list[str]] = {
    "breaking":      ["#BreakingNews", "#WorldNews"],
    "war":           ["#War", "#Conflict"],
    "world":         ["#WorldNews", "#International"],
    "politics":      ["#Politics", "#Government"],
    "crime":         ["#Crime", "#Justice"],
    "technology":    ["#Technology", "#Tech"],
    "business":      ["#Business", "#Economy"],
    "science":       ["#Science", "#Discovery"],
    "sports":        ["#Sports"],
    "entertainment": ["#Entertainment"],
    "climate":       ["#ClimateChange", "#Environment"],
    "wellness":      ["#Health", "#Wellness"],
    "jobs":          ["#Jobs", "#Employment"],
    "trending":      ["#Trending"],
}

# High-value keyword → hashtag mapping (order matters — first match wins)
_KEYWORD_HASHTAGS: list[tuple[str, str]] = [
    # ── South Asia ────────────────────────────────────────────────────────
    (r"\bpakistan\b",                       "#Pakistan"),
    (r"\bindia\b",                          "#India"),
    (r"\bkashmir\b",                        "#Kashmir"),
    (r"\bkarachi\b",                        "#Karachi"),
    (r"\blahore\b",                         "#Lahore"),
    (r"\bislamabad\b",                      "#Islamabad"),
    (r"\bnew delhi\b|\bdelhi\b",            "#Delhi"),
    (r"\bmumbai\b",                         "#Mumbai"),
    (r"\bpti\b",                            "#PTI"),
    (r"\bimran khan\b",                     "#ImranKhan"),
    (r"\bnawaz sharif\b",                   "#NawazSharif"),
    (r"\bashraf ghani\b|\bshehbaz\b",       "#Pakistan"),
    (r"\bdefence day\b|defense day\b",      "#DefenceDay"),
    (r"\bnepal\b",                          "#Nepal"),
    (r"\bbangladesh\b",                     "#Bangladesh"),
    (r"\bsri lanka\b",                      "#SriLanka"),
    (r"\bafghanistan\b",                    "#Afghanistan"),
    (r"\bbalochistan\b",                    "#Balochistan"),
    (r"\bsindh\b",                          "#Sindh"),
    (r"\bkhyber\b|\bkpk\b",                "#KPK"),
    (r"\bmodi\b",                           "#Modi"),
    # ── Middle East ───────────────────────────────────────────────────────
    (r"\biran\b",                           "#Iran"),
    (r"\bisrael\b",                         "#Israel"),
    (r"\blebanon\b",                        "#Lebanon"),
    (r"\bpalestine\b|\bgaza\b",             "#Gaza"),
    (r"\bhezbollah\b",                      "#Hezbollah"),
    (r"\bhamas\b",                          "#Hamas"),
    (r"\bsaudi arabia\b|\bsaudi\b",         "#SaudiArabia"),
    (r"\byemen\b",                          "#Yemen"),
    (r"\biraq\b",                           "#Iraq"),
    (r"\bsyria\b",                          "#Syria"),
    (r"\bmiddle east\b",                    "#MiddleEast"),
    # ── Europe / Russia ───────────────────────────────────────────────────
    (r"\bukraine\b",                        "#Ukraine"),
    (r"\brussia\b",                         "#Russia"),
    (r"\bputin\b",                          "#Putin"),
    (r"\bzelensky\b",                       "#Zelensky"),
    (r"\bnato\b",                           "#NATO"),
    (r"\bgermany\b|\bgerman\b",             "#Germany"),
    (r"\bfrance\b|\bfrench\b",              "#France"),
    (r"\buk\b|\bbritain\b|\bbritish\b",     "#UK"),
    (r"\bscotland\b",                       "#Scotland"),
    (r"\beurope\b|\beuropean\b",            "#Europe"),
    (r"\bpoland\b",                         "#Poland"),
    (r"\bafD\b",                            "#AfD"),
    # ── Americas ──────────────────────────────────────────────────────────
    (r"\busa\b|\bunited states\b|\bu\.s\b", "#USA"),
    (r"\btrump\b",                          "#Trump"),
    (r"\bbiden\b",                          "#Biden"),
    (r"\bwhite house\b",                    "#WhiteHouse"),
    (r"\bcongress\b",                       "#UsCongress"),
    (r"\bcanada\b",                         "#Canada"),
    (r"\bmexico\b",                         "#Mexico"),
    (r"\bbrazil\b",                         "#Brazil"),
    # ── Asia Pacific ──────────────────────────────────────────────────────
    (r"\bchina\b|\bchinese\b",              "#China"),
    (r"\bjapan\b|\bjapanese\b",             "#Japan"),
    (r"\bsouth korea\b|\bkorea\b",          "#Korea"),
    (r"\bnorth korea\b",                    "#NorthKorea"),
    (r"\baustralia\b|\baustralian\b",       "#Australia"),
    (r"\bindonesia\b",                      "#Indonesia"),
    (r"\bphilippines\b",                    "#Philippines"),
    (r"\bthailand\b",                       "#Thailand"),
    # ── Africa ────────────────────────────────────────────────────────────
    (r"\bafrica\b|\bafrican\b",             "#Africa"),
    (r"\bsouth africa\b",                   "#SouthAfrica"),
    (r"\bnigeRia\b|\bnigerian\b",           "#Nigeria"),
    (r"\bethiopia\b",                       "#Ethiopia"),
    (r"\bkenya\b",                          "#Kenya"),
    # ── Global institutions ───────────────────────────────────────────────
    (r"\bun\b|\bunited nations\b",          "#UnitedNations"),
    (r"\bimf\b",                            "#IMF"),
    (r"\bworld bank\b",                     "#WorldBank"),
    (r"\bwho\b|world health organization",  "#WHO"),
    # ── Economy / Finance ─────────────────────────────────────────────────
    (r"\beconomy\b|\brecession\b|\binflation\b", "#Economy"),
    (r"\bwall street\b|\bstock market\b",   "#WallStreet"),
    (r"\bfederal reserve\b|\bfed\b",        "#FederalReserve"),
    (r"\boil\b|\bopec\b",                   "#Oil"),
    (r"\bcrypto\b|\bbitcoin\b",             "#Crypto"),
    (r"\btrade war\b|\btariff\b",           "#TradeWar"),
    (r"\bsanctions\b",                      "#Sanctions"),
    # ── Technology ────────────────────────────────────────────────────────
    (r"\bartificial intelligence\b|\bai\b", "#ArtificialIntelligence"),
    (r"\bcybersecurity\b|\bcyber attack\b", "#Cybersecurity"),
    (r"\bapple\b",                          "#Apple"),
    (r"\bgoogle\b",                         "#Google"),
    (r"\bmeta\b|\bfacebook\b",              "#Meta"),
    (r"\bmicrosoft\b",                      "#Microsoft"),
    (r"\belon musk\b",                      "#ElonMusk"),
    (r"\btesla\b",                          "#Tesla"),
    (r"\bspacex\b",                         "#SpaceX"),
    (r"\bamazon\b",                         "#Amazon"),
    # ── Science / Space ───────────────────────────────────────────────────
    (r"\bspace\b|\bnasa\b",                 "#Space"),
    (r"\bnuclear\b",                        "#Nuclear"),
    (r"\bclimate\b|\bglobal warming\b",     "#ClimateChange"),
    (r"\bearthquake\b",                     "#Earthquake"),
    (r"\bflood\b|\bflooding\b",             "#Floods"),
    (r"\bhurricane\b|\btyphoon\b|\bcyclone\b", "#NaturalDisaster"),
    (r"\bvolcano\b|\beruption\b",           "#Volcano"),
    # ── Health ────────────────────────────────────────────────────────────
    (r"\bcovid\b|\bpandemic\b",             "#Covid"),
    (r"\bvaccine\b|\bvaccination\b",        "#Vaccine"),
    (r"\bcancer\b",                         "#Cancer"),
    # ── Conflict / Military ───────────────────────────────────────────────
    (r"\bceasefire\b",                      "#Ceasefire"),
    (r"\bwar\b|\bconflict\b",               "#War"),
    (r"\bterror\b|\bterrorism\b|\bterrorist\b", "#Terrorism"),
    (r"\bmilitary\b|\bairstrikes?\b",       "#Military"),
    # ── Politics ─────────────────────────────────────────────────────────
    (r"\belection\b|\bvote\b|\bpoll\b",     "#Election"),
    (r"\bsupreme court\b",                  "#SupremeCourt"),
    (r"\bcoup\b",                           "#Coup"),
    # ── Sports ───────────────────────────────────────────────────────────
    (r"\bfootball\b|\bsoccer\b|\bpremier league\b", "#Football"),
    (r"\bcricket\b",                        "#Cricket"),
    (r"\bolympic\b",                        "#Olympics"),
    (r"\bwimbledon\b|\btennis\b",           "#Tennis"),
    (r"\bworld cup\b",                      "#WorldCup"),
]


def _generate_hashtags(story: Story) -> list[str]:
    """
    Generate 3–5 relevant hashtags for a story.
    Always includes #GlobalPulseNews.

    Priority order:
      1. #GlobalPulseNews (always first)
      2. Story-specific keyword tags (matched from title + summary)
      3. Named entity tags extracted from title (countries, people, orgs)
      4. Category base tag (only if still under 3 tags)
      5. Generic fallback (only to reach minimum 3)

    Never adds a generic tag if a specific one already fills the slot.
    """
    tags: list[str] = ["#GlobalPulseNews"]

    text     = ((story.title or "") + " " + (story.raw_summary or "")).lower()
    title    = (story.title or "").lower()
    category = getattr(story, "category", "breaking")

    # ── Step 1: keyword-matched story-specific tags ───────────────────────
    for pattern, tag in _KEYWORD_HASHTAGS:
        if len(tags) >= 5:
            break
        if tag not in tags and re.search(pattern, text, re.IGNORECASE):
            tags.append(tag)

    # ── Step 2: named entity extraction from title ────────────────────────
    # Match exactly 2-word proper nouns (e.g. "Elon Musk", "New Delhi")
    # Skip if either word is a common news noun
    if len(tags) < 5:
        _common = {
            "Building", "Collapse", "Attack", "Arrest", "Killed", "Kills", "Dead",
            "Death", "Crash", "Fire", "Flood", "Storm", "Strike", "Crisis", "Deal",
            "Plan", "Vote", "Poll", "Trial", "Claim", "Call", "Talk", "Warn",
            "Warning", "Warns", "Blast", "Bomb", "Leader", "Minister", "President",
            "Government", "Official", "Police", "Court", "Party", "State", "Nation",
            "Country", "People", "Group", "Force", "Army", "Military", "Security",
            "House", "Meet", "Meeting", "Summit", "Bill", "Case", "School",
            "Hospital", "Bank", "Trade", "Market", "Price", "Report", "Survey",
            "News", "World", "Global", "Breaking", "Latest", "Update", "Watch",
            "Says", "Said", "Wins", "Loses", "Fears", "Faces", "Seeks", "Backs",
            "Dozens", "Hundreds", "Thousands", "Millions", "Billions",
            "Trapped", "Killed", "Wounded", "Missing", "Rescued", "Freed",
            "First", "Second", "Third", "After", "Before", "Since", "Every",
        }
        pairs = re.findall(r'\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b', story.title or "")
        for w1, w2 in pairs:
            if len(tags) >= 5:
                break
            if w1 in _common or w2 in _common:
                continue
            candidate = f"#{w1}{w2}"
            if candidate not in tags:
                already_covered = any(w1.lower() in t.lower() or w2.lower() in t.lower() for t in tags)
                if not already_covered:
                    tags.append(candidate)

    # ── Step 3: category base tag (only to reach minimum 3) ──────────────
    if len(tags) < 3:
        for tag in _CATEGORY_HASHTAGS.get(category, ["#WorldNews"]):
            if len(tags) >= 4:
                break
            if tag not in tags:
                tags.append(tag)

    # ── Step 4: generic fallback (only to reach minimum 3) ───────────────
    fallbacks = ["#WorldNews", "#BreakingNews", "#GlobalNews", "#News"]
    for fb in fallbacks:
        if len(tags) >= 3:
            break
        if fb not in tags:
            tags.append(fb)

    # Cap at 5
    return tags[:5]


# ---------------------------------------------------------------------------
# Legacy shim — kept so nothing else breaks
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> tuple[str, list[str]]:
    """Kept for backwards compat. Use _parse_post_body + _generate_hashtags instead."""
    return _parse_post_body(raw), []
