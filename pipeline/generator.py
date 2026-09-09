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

# A scheduled fetch run uses one Python process. After an infrastructure-level
# LLM failure, avoid repeating the same expensive call for every selected story.
_GENERATION_BACKEND_UNAVAILABLE: str | None = None


def _backend_error_summary(exc: Exception) -> str:
    """Keep infrastructure failures useful without repeating large HTTP bodies."""
    first_line = str(exc).splitlines()[0] if str(exc) else "unknown error"
    return f"{type(exc).__name__}: {first_line[:180]}"


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
    Raises GenerationError only if Ollama returns truly nothing (completely empty).

    Philosophy — fix it, don't reject it:
    - Enrich thin summaries before sending to Ollama
    - Accept short posts for genuinely short stories
    - Only retry if Ollama returned nothing at all
    - Hashtags always generated deterministically
    """
    global _GENERATION_BACKEND_UNAVAILABLE

    from openai import OpenAIError
    from pipeline.card_headline import select_card_headline
    from pipeline.card_description import select_card_description
    from pipeline.post_fact_checker import check_generated_facts

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

    if _GENERATION_BACKEND_UNAVAILABLE:
        logger.warning(
            "LLM backend remains unavailable for this run (%s) — using deterministic grounded caption.",
            _GENERATION_BACKEND_UNAVAILABLE,
        )
        return _apply_grounded_fallback(story)

    summary_len = len(story.raw_summary or "")
    # Absolute minimum we'll accept — purely empty responses get retried
    # Short stories (thin summary) → accept 60+ chars
    # Medium stories → accept 80+ chars
    # Rich stories → accept 150+ chars, but don't reject if Ollama gives less
    hard_min = 60  # anything under this is treated as "Ollama returned nothing"

    last_error: Exception | None = None
    best_output: str = ""  # track best attempt so far
    retry_reason = "previous generation was invalid"

    for attempt in range(1, MAX_GENERATION_RETRIES + 1):
        if attempt > 1:
            logger.info("Retrying generation (attempt %d/%d) — %s.",
                        attempt, MAX_GENERATION_RETRIES, retry_reason)

        prompt = _build_prompt(story)

        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.35,
            )
        except OpenAIError as exc:
            _GENERATION_BACKEND_UNAVAILABLE = _backend_error_summary(exc)
            logger.error(
                "LLM call failed (%s): %s — switching this run to deterministic grounded captions.",
                config.LLM_BACKEND,
                _GENERATION_BACKEND_UNAVAILABLE,
            )
            break
        except Exception as exc:  # noqa: BLE001
            _GENERATION_BACKEND_UNAVAILABLE = _backend_error_summary(exc)
            logger.error(
                "LLM call failed (%s): %s — switching this run to deterministic grounded captions.",
                config.LLM_BACKEND,
                _GENERATION_BACKEND_UNAVAILABLE,
            )
            break

        raw_output = response.choices[0].message.content or ""
        logger.debug("LLM raw output:\n%s", raw_output)

        post_content = _parse_post_body(raw_output)

        if not post_content or len(post_content) < hard_min:
            # Keep the best we've seen so far (longest non-empty output)
            if post_content and len(post_content) > len(best_output):
                best_output = post_content
            last_error = GenerationError(f"LLM returned too little content ({len(post_content)} chars).")
            retry_reason = "previous output was too short"
            continue

        # Good enough — accept it regardless of length
        if summary_len > 200 and len(post_content) < 150:
            logger.warning(
                "Post is shorter than ideal for a rich story (%d chars) — publishing anyway.",
                len(post_content),
            )

        hashtags = _generate_hashtags(story)
        story.card_headline = select_card_headline(story, raw_output)
        story.card_description = select_card_description(story, raw_output)
        post_content = _format_post(post_content, story)
        fact_check = check_generated_facts(story, post_content)
        if not fact_check.passed:
            last_error = GenerationError("Generated caption failed fact grounding: " + "; ".join(fact_check.issues[:4]))
            retry_reason = "previous caption failed fact grounding"
            logger.warning("Caption fact-check failed (attempt %d/%d): %s", attempt, MAX_GENERATION_RETRIES, last_error)
            continue

        story.post_content  = post_content
        story.hashtags      = hashtags
        logger.info("Post generated (%d chars). Card: %s | Hashtags: %s",
                    len(post_content), story.card_headline, " ".join(hashtags))
        return story

    # Never lose a verified story because the writing model repeatedly chose a
    # bad paraphrase. Build a conservative post entirely from source sentences.
    logger.warning(
        "All %d model attempts failed — using deterministic grounded caption.",
        MAX_GENERATION_RETRIES,
    )
    return _apply_grounded_fallback(story)


def _apply_grounded_fallback(story: Story) -> Story:
    """Build and verify a caption without depending on the writing model."""
    from pipeline.post_fact_checker import check_generated_facts

    story.card_headline = _fallback_card_headline(story)
    story.card_description = None
    story.post_content = _grounded_fallback_post(story)
    story.hashtags = _generate_hashtags(story)
    fallback_check = check_generated_facts(story, story.post_content)
    if fallback_check.passed:
        logger.info("Deterministic grounded caption created for: %s", story.title)
        return story

    raise GenerationError(
        "Deterministic fallback failed fact grounding: " + "; ".join(fallback_check.issues[:4])
    )


def reset_generation_backend_state() -> None:
    """Reset the LLM circuit breaker at the start of a new fetch run."""
    global _GENERATION_BACKEND_UNAVAILABLE
    _GENERATION_BACKEND_UNAVAILABLE = None


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

    # Already rich enough and deep verification found no additional body text.
    if len(summary) >= 200 and not story.article_text:
        return story

    enriched_parts: list[str] = [summary]

    # Deep verification may have extracted the public article body. It is kept
    # separate from the feed summary for auditability, then supplied as context.
    if story.article_text and story.article_text not in summary:
        enriched_parts.append(story.article_text)

    # Pull from corroborating sources if they carry extra text
    if story.corroborating_sources:
        for src in story.corroborating_sources:
            extra = (
                src.get("article_text")
                or src.get("description")
                or src.get("content")
                or src.get("summary")
                or ""
            )
            if extra and extra not in summary:
                enriched_parts.append(extra)

    combined = " ".join(p.strip() for p in enriched_parts if p.strip())[:6_000]

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
- Add one natural, story-specific question only when it improves understanding.
- Never ask people to like, share, comment, tag others, or "drop" an opinion.

FIRST TWO LINES ARE CRITICAL — THE HOOK:
Facebook commonly shows only the opening lines before "See more". Make both lines specific and compelling.
Line 1: Begin with exactly ONE relevant symbol, then the strongest verified fact in under 12 words.
         Examples:
         "🔴 BREAKING: Amazon cargo plane crashes at Miami airport — 5 dead."
         "⚡ JUST IN: Germany's far-right AfD wins landslide in eastern states."
         "📌 DEVELOPING: Delhi building collapse — 6 killed, dozens still trapped."
         "🌍 WORLD: Pakistan formally rejects India's claims over Kashmir."
Line 2: Start with "Why this matters:" and give one grounded sentence explaining the consequence or stakes.
Line 3 (optional): One key detail or number that adds weight.

At the very end of the post, always include:
📰 Sources: <comma-separated source names>

Output format (return exactly this structure, nothing else):
CARD_HEADLINES:
<Write exactly 3 alternative card headlines, numbered 1–3. Each must be 4–9 words, active voice, a complete thought, and understandable in one second on a phone.

1. FACT-LED: lead with the strongest verified fact or number.
2. IMPACT-LED: show who is affected or why the update matters.
3. ACTION-LED: lead with the main person/country and strongest accurate verb.

Use only details explicitly present in the supplied evidence. Prefer a specific person, country, number, or consequence. Never use questions, teasers, vague pronouns, unsupported adjectives, or clickbait such as SHOCKING and YOU WON'T BELIEVE.

BAD (cut off): "75% OF A&E STAFF IN UK FACE"
GOOD: "75% OF NHS STAFF FACE DAILY VIOLENCE"

BAD (cut off): "GERMANY AFD WINS HISTORIC LANDSLIDE IN EASTERN"
GOOD: "AFD WINS GERMANY LANDSLIDE — FAR-RIGHT SURGES"

BAD (cut off): "DELHI BUILDING COLLAPSE KILLS 6, DOZENS STILL"
GOOD: "6 DEAD AS DELHI BUILDING COLLAPSES"

BAD (vague): "PAKISTAN REJECTS INDIA BASELESS CLAIMS ON OCCUPIED"
GOOD: "PAKISTAN REJECTS INDIA'S KASHMIR CLAIMS"

Write three punchy complete statements, not truncated titles.>

CARD_DESCRIPTION:
<Write one complete sentence of 7–18 words. It must add the strongest verified
consequence or key detail, remain clearly relevant to the same story, and be
different from all card headlines. Do not repeat the title, ask a question,
use clickbait, or introduce any fact absent from the supplied evidence.>

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
        VerificationStatus.VERIFIED: (
            story.verification_reason
            or "This story has consistent reporting from independent reliable sources."
        ),
        VerificationStatus.UNVERIFIED: (
            (story.verification_reason or "This story has not been independently confirmed.")
            + " Label unconfirmed claims appropriately."
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

MANDATORY OPENING:
Line 1 starts with exactly one symbol and the strongest verified fact.
Line 2 explains the immediate consequence or why the fact matters.
Do not place a blank line between these two lines. Do not add extra symbols to them.

Example for this story:
"{hook_label}: {story.title[:60]}{'...' if len(story.title) > 60 else ''}"

The post MUST follow this structure:
LINE 1: {cat_emoji} [single most striking verified fact — under 12 words]
LINE 2: Why this matters: [one consequence or stake supported by the supplied evidence]
LINE 3 (optional): [one key detail or number that adds weight]
[BLANK LINE]
BODY: 2-3 short paragraphs (max 2-3 sentences each), blank line between each.
OPTIONAL QUESTION: Include only a specific, natural question that adds civic or practical context. Omit it when it would be generic. Never request likes, shares, comments, tags, or reactions, and do not add 👇.
SOURCES: 📰 Sources: {', '.join(unique_source_names)}

Write the post now."""


# ---------------------------------------------------------------------------
# Post formatting — enforce a grounded two-line opening
# ---------------------------------------------------------------------------

_GENERIC_ENGAGEMENT_PROMPT = re.compile(
    r"^(?:what do you think(?: about this)?|what is your view on this development|"
    r"how do you see this unfolding|what(?:'s| is) your take on this)\??"
    r"(?:\s*(?:share your thoughts|drop your opinion|let us know|comment)\s*(?:below)?)?\s*👇?$",
    re.IGNORECASE,
)


def _grounded_fallback_post(story: Story) -> str:
    """Build readable Facebook copy using exact sentences from verified evidence."""
    evidence_blocks = [story.raw_summary or "", story.article_text or ""]
    evidence_blocks.extend(
        source.get("summary", "") for source in story.corroborating_sources or []
    )
    sentences: list[str] = []
    seen: set[str] = set()
    for block in evidence_blocks:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", block):
            sentence = re.sub(r"<[^>]+>", " ", sentence)
            sentence = re.sub(r"\s+", " ", sentence).strip()
            key = sentence.lower().rstrip(".!?")
            if len(sentence.split()) < 6 or key in seen or sentence.endswith("?"):
                continue
            seen.add(key)
            sentences.append(sentence.rstrip() if sentence.endswith((".", "!")) else sentence + ".")
            if len(sentences) == 3:
                break
        if len(sentences) == 3:
            break

    context = sentences[0] if sentences else story.title
    detail = "\n\n".join(sentences[2:] if len(sentences) > 1 else [])
    sources = [story.source_name]
    sources.extend(source.get("name", "") for source in story.corroborating_sources or [])
    sources = list(dict.fromkeys(name for name in sources if name))
    why_it_matters = sentences[1] if len(sentences) > 1 else context
    raw = f"{story.card_headline}\nWhy this matters: {why_it_matters}"
    if detail:
        raw += f"\n\n{detail}"
    raw += f"\n\n📰 Sources: {', '.join(sources)}"
    return _format_post(raw, story)

def _format_post(post: str, story: Story | None = None) -> str:
    """Build a card-matched two-line hook followed by a readable body."""
    post = re.sub(r"\n{3,}", "\n\n", post.strip())
    if story is None:
        return post

    symbol = _CATEGORY_EMOJI.get(getattr(story, "category", "world"), "🌍")
    card = (story.card_headline or _fallback_card_headline(story)).strip()
    card = re.sub(r"^[^A-Za-z0-9]+", "", card).strip()
    hook_line = f"{symbol} {card}"

    lines = [line.strip() for line in post.splitlines() if line.strip()]
    cleaned_lines: list[str] = []
    for line in lines:
        line = re.sub(r"^[^A-Za-z0-9]+", "", line).strip()
        line = re.sub(
            r"^(?:BREAKING|JUST IN|DEVELOPING|WORLD|WAR UPDATE|POLITICS|TECH|BUSINESS|SPORTS|CRIME|CLIMATE|SCIENCE|ENTERTAINMENT|HEALTH|JOBS)\s*:\s*",
            "", line, flags=re.I,
        ).strip()
        if line and not _GENERIC_ENGAGEMENT_PROMPT.match(line):
            cleaned_lines.append(line)

    # The generated first line is usually another headline. Use the next
    # grounded sentence as the consequence/context line.
    context_candidates = cleaned_lines[1:] or cleaned_lines
    context = next(
        (line for line in context_candidates
         if not line.lower().startswith(("sources:", "source:"))
         and "?" not in line
         and line.lower() not in card.lower()
         and card.lower() not in line.lower()),
        "",
    )
    if not context:
        context = (story.raw_summary or story.title).split(".")[0].strip()
    original_context = context
    if not context.lower().startswith("why this matters:"):
        context = f"Why this matters: {context}"

    body_lines = [line for line in cleaned_lines if line != original_context]
    if body_lines and body_lines[0].lower() in card.lower():
        body_lines.pop(0)
    body = "\n\n".join(body_lines)

    opening = f"{hook_line}\n{context}"
    return opening + (f"\n\n{body}" if body else "")


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
