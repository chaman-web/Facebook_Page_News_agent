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
from pipeline.caption_sanitizer import sanitize_facebook_caption

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

        prompt = _build_prompt(story, retry_hint=retry_reason if attempt > 1 else None)

        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.60,
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

        # Hybrid hashtag strategy -- single source of truth via _build_hashtags:
        # 1. Parse LLM tags from the full post response
        # 2. If 2+ valid tags: use them + #GlobalPulseNews
        # 3. If < 2 valid: try a focused hashtag-only LLM call
        # 4. Last resort: deterministic keyword generator
        llm_tags = _parse_llm_hashtags(raw_output)
        if len(llm_tags) < 2:
            llm_tags = _fetch_llm_hashtags(story)
        hashtags = _build_hashtags(story, llm_tags)
        story.card_headline = select_card_headline(story, raw_output)
        story.card_description = select_card_description(story, raw_output)
        post_content = sanitize_facebook_caption(_format_post(post_content, story))
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


# ---------------------------------------------------------------------------
# Dedicated LLM hashtag fetch -- used when full generation was skipped/failed
# ---------------------------------------------------------------------------

_HASHTAG_ONLY_PROMPT = (
    "You are a social media hashtag specialist for a Facebook news page.\n"
    "Given a news story title and category, generate exactly 4 hashtags.\n"
    "\n"
    "Rules:\n"
    "  - Each hashtag must start with #\n"
    "  - No spaces inside a hashtag\n"
    "  - Must be real, searchable Facebook hashtags people actually follow\n"
    "  - Must be SPECIFIC to this exact story (person, place, event, topic)\n"
    "  - Do NOT use: #News #Update #Latest #Breaking #Trending #GlobalPulseNews\n"
    "  - Do NOT invent compound tags: no #MuslimDemocrats #USDefense #WarInIran\n"
    "\n"
    "Respond with ONLY the 4 hashtags on a single line, space-separated.\n"
    "Example: #Gaza #Israel #MiddleEast #Ceasefire\n"
    "Example: #JeremyThomas #BritishFilm #FilmProducer #Cinema\n"
    "Example: #Democrats #Populism #USPolitics #Elections\n"
)


def _fetch_llm_hashtags(story: Story) -> list[str]:
    """Ask the LLM for story-specific hashtags using a minimal focused prompt.

    Used when the full generation path was skipped or failed, so posts never
    get static keyword-matched tags when a live LLM is available.

    Returns a validated list of 2-4 hashtags, or [] if LLM is unavailable
    or returns unusable output. Never raises.
    """
    # Respect the circuit breaker -- if Ollama is already confirmed down, skip
    if _GENERATION_BACKEND_UNAVAILABLE:
        return []

    try:
        client = _get_client()
        category = getattr(story, "category", "world")
        user_msg = (
            f"TITLE: {story.title}\n"
            f"CATEGORY: {category}\n"
            f"SUMMARY (first sentence): "
            f"{(story.raw_summary or '').split('.')[0].strip()}"
        )
        _model = config.OLLAMA_MODEL if config.LLM_BACKEND == "ollama" else config.OPENAI_MODEL
        response = client.chat.completions.create(
            model=_model,
            messages=[
                {"role": "system", "content": _HASHTAG_ONLY_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.5,
            timeout=10,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Focused call returns a bare tag line (no HASHTAGS: block header).
        # Validate tags directly from the raw line.
        candidates = re.findall(r"#[A-Za-z][A-Za-z0-9_]{1,29}", raw)
        tags: list[str] = []
        for tag in candidates:
            lower = tag.lower()
            if lower in _BLOCKED_HASHTAGS:
                continue
            inner = tag[1:]
            upper_count = sum(1 for c in inner if c.isupper())
            _known = {
                "saudiarabia", "celinedion", "northkorea", "southkorea",
                "southafrica", "middleeast", "unitednations", "elonmusk",
                "worldbank", "federalreserve", "wallstreet", "newzealand",
                "srilanka", "humanrights", "climatechange", "naturaldisaster",
                "stockmarket", "whitehouse", "stiffpersonsyndrome",
            }
            if upper_count >= 2 and len(inner) > 7 and inner.lower() not in _known:
                continue
            if tag not in tags:
                tags.append(tag)
            if len(tags) >= 4:
                break
        if len(tags) >= 2:
            logger.debug("LLM hashtags (focused call): %s", tags)
            return tags
        return []
    except Exception:  # noqa: BLE001
        return []  # never block the pipeline


def _build_hashtags(story: Story, llm_tags: list[str]) -> list[str]:
    """Build the final hashtag list from LLM tags + deterministic fallback.

    Single source of truth for hashtag assembly used by all paths:
      - Full LLM generation path (llm_tags from post response)
      - Focused hashtag-only LLM call (llm_tags from _fetch_llm_hashtags)
      - Pure deterministic fallback (llm_tags=[])

    Always:
      - Starts with #GlobalPulseNews
      - Returns 3-5 tags total
      - Uses deterministic generator to fill gaps if LLM gave < 2 valid tags
    """
    if len(llm_tags) >= 2:
        # LLM gave good tags -- use them, strip any accidental #GlobalPulseNews
        clean = [t for t in llm_tags if t.lower() != "#globalpulsenews"]
        return ["#GlobalPulseNews"] + clean[:4]
    # Deterministic fallback
    return _generate_hashtags(story)


def _apply_grounded_fallback(story: Story) -> Story:
    """Build and verify a caption without depending on the writing model."""
    from pipeline.post_fact_checker import check_generated_facts

    story.card_headline = _fallback_card_headline(story)
    story.card_description = None
    story.post_content = _grounded_fallback_post(story)
    # Try LLM for story-specific hashtags even on the fallback path
    story.hashtags = _build_hashtags(story, _fetch_llm_hashtags(story))
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
- Separate paragraphs with one empty line. Never print formatting instructions such as "blank line".
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
End with a Sources line containing the supplied source names.

Output format (return exactly this structure, nothing else):
CARD_HEADLINES:
Write exactly 3 alternative card headlines, numbered 1–3.
Target 8–14 words. Each headline MUST:
  - Use active voice
  - Be a COMPLETE thought: subject + verb + conclusion
  - NEVER stop mid-sentence or trail off
  - End on a word that completes the thought. NEVER end on:
    prepositions (in, of, for, with, as, by, from, into, over, after, amid),
    articles (the, a, an), conjunctions (and, or, but),
    pronouns (us, it, he, she, they, its),
    or subject nouns without a predicate (missile, missiles, world, capital,
    canyon, plan, report, situation, matter, case, thing)
  - NOT start with label prefixes like FACT-LED:, IMPACT-LED:, ACTION-LED:
    (those are writing guides only, remove them from your output)

1. FACT-LED: lead with the strongest verified fact or number.
2. IMPACT-LED: show who is affected or why the update matters.
3. ACTION-LED: lead with the main person/country and strongest accurate verb.

Use only details explicitly present in the supplied evidence. Prefer a specific
person, country, number, or consequence. Never use questions, teasers, vague
pronouns, unsupported adjectives, or clickbait such as SHOCKING and YOU WON'T BELIEVE.

BAD (cut off mid-sentence): "BRICS LEADERS MEET IN INDIA AS WARS AND US"
GOOD: "BRICS LEADERS MEET IN INDIA AMID WAR AND US TENSIONS"

BAD (ends on subject noun, no predicate): "GROUP IN YEMEN USED CLAUDE AI FOR MISSILE"
GOOD: "YEMEN GROUP USED CLAUDE AI TO DEVELOP MISSILES"

BAD (no verb, incomplete): "MAYOR OF THE AIR CAPITAL OF THE WORLD"
GOOD: "WICHITA MAYOR PUSHES BACK ON TRUMP AIRPORT THREAT"

BAD (cut off): "75% OF A&E STAFF IN UK FACE"
GOOD: "75% OF NHS STAFF FACE DAILY VIOLENCE"

BAD (cut off): "DELHI BUILDING COLLAPSE KILLS 6, DOZENS STILL"
GOOD: "6 DEAD AS DELHI BUILDING COLLAPSES"

BAD (label prefix in output): "FACT-LED: NEPRA APPROVES POWER PLAN"
GOOD: "NEPRA APPROVES 58 BILLION DOLLAR POWER PLAN"

Write three punchy, self-contained statements. Each must make complete sense
on its own without reading the article.

CARD_DESCRIPTION:
Write one complete sentence of 7–18 words that makes a reader stop scrolling
and want to read more. Rules:
  - Must contain a SPECIFIC detail: a number, a named person/place, or a direct quote
  - Must show CONSEQUENCE or HUMAN IMPACT — what happened to real people or what changes
  - Must be clearly about the same story as the title (share at least 2 key words)
  - Must be different from the card headlines — add new information
  - Do NOT repeat the title, ask a question, use vague language like
    "significant challenges", "major concerns", or "could face issues"
  - Do NOT use clickbait or introduce any fact absent from the supplied evidence

BAD: "Sweden's future government could face significant challenges balancing priorities."
GOOD: "Over 3 million Swedes rely on welfare services now threatened by defence cuts."

BAD: "The situation remains developing and officials are monitoring the crisis."
GOOD: "Rescue teams pulled 47 survivors from rubble as families waited through the night."

BAD: "Experts say the decision may have a serious impact on affected communities."
GOOD: "The sanctions freeze £200 million in West Bank settlement trade immediately."

HASHTAGS:
Write exactly 4 hashtags directly related to this specific story.
Rules:
  - Each must start with #, no spaces inside the tag
  - Must be real, searchable Facebook hashtags that people actually follow
  - Must relate to THIS story: the specific person, place, event, or topic
  - Do NOT use generic fillers: #News, #Update, #Latest, #Breaking, #Trending
  - Do NOT invent compound tags: never #MuslimDemocrats, #WorldLeaders, #UsCongress
  - Do NOT include #GlobalPulseNews (added automatically)
  - Use specific well-known tags: #Gaza, #Trump, #Earthquake, #BRICS, #CelineDion

BAD: #MuslimDemocrats #WorldLeaders #GlobalNews #BreakingUpdate
GOOD: #Politics #Democrats #USA #Elections

BAD: #WHO #Military #News #Update
GOOD: #Iraq #Corruption #MiddleEast #Justice

Write the 4 hashtags on a single line, space-separated. Example:
#Gaza #Israel #MiddleEast #Ceasefire

After the POST label, write the complete Facebook post without template markers or HTML tags.
POST:
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


# ---------------------------------------------------------------------------
# Summary compression -- policy-aligned, fact-preserving
# Aligned with Facebook's April 2025 anti-spam enforcement:
#   - Never produce padded, low-quality captions
#   - Every sentence must add a distinct verified fact
#   - Never drop a sentence that contains a number, named person/place/org
# ---------------------------------------------------------------------------

# Sentence-level boilerplate detector -- drops entire sentences that are pure noise
_SENT_NOISE = re.compile(
    r"^(?:"
    r"read\s+(?:more|full|article)"
    r"|click(?:\s+here)?"
    r"|subscribe(?:\s+(?:to|for|now))?"
    r"|sign\s+up"
    r"|full\s+coverage"
    r"|more\s+stories"
    r"|follow\s+us"
    r"|visit\s+(?:our|the)\s+(?:website|site|page)"
    r"|get\s+the\s+(?:latest|full)"
    r"|copyright\b"
    r"|\(c\)\s"
    r"|\xa9"
    r"|this\s+(?:story|article|report)\s+(?:has been|will be|is being)\s+updated"
    r"|developing\s+story"
    r"|this\s+is\s+a\s+developing\s+story"
    r")\b",
    re.IGNORECASE,
)

# Filler sentences that add zero information and can be dropped if over limit
_FILLER_SENT = re.compile(
    r"^(?:"
    r"in\s+(?:related|other)\s+news"
    r"|meanwhile"
    r"|it\s+(?:has\s+been|was)\s+(?:widely\s+)?reported\s+that"
    r"|according\s+to\s+(?:multiple\s+)?(?:sources|reports|officials)"
    r"|sources\s+(?:say|said|told|have\s+confirmed)"
    r")\b",
    re.IGNORECASE,
)

_SOFT_WORD_LIMIT = 200   # target word count; never cut below _MIN_WORDS
_MIN_WORDS       = 80    # never compress below this -- return more if needed


def _is_fact_bearing(sentence: str) -> bool:
    """Return True if a sentence carries at least one concrete verifiable fact.

    Fact-bearing sentences are NEVER dropped during trimming.
    A sentence is fact-bearing if it contains:
      - A number or quantity (12, 3.5 million, 40%)
      - A proper noun (capitalised mid-sentence word, length > 2)
      - An ALL-CAPS acronym (WHO, NATO, GDP)
      - A direct quote
    """
    if re.search(r"\b\d+(?:[,./]\d+)?\s*(?:million|billion|thousand|percent|%|km|kg|mph)?", sentence):
        return True
    words = sentence.split()
    for i, w in enumerate(words[1:], 1):
        clean = re.sub(r"[^A-Za-z]", "", w)
        if clean and clean[0].isupper() and len(clean) > 2:
            return True
    if re.search(r"\b[A-Z]{2,}\b", sentence):
        return True
    if re.search(r'["\u201c\u2018].{5,}["\u201d\u2019]', sentence):
        return True
    return False


def _compress_summary(text: str) -> str:
    """Return a clean, compact, Facebook policy-aligned version of a news summary.

    Strategy (preserve-then-trim):
      1. Split into sentences immediately
      2. Drop known boilerplate sentences (ads, subscribe, copyright)
      3. Deduplicate near-identical sentences (keep first occurrence)
      4. If under 150 words -- return cleaned result as-is
      5. Mark each remaining sentence as fact-bearing or filler
      6. Always keep fact-bearing sentences
      7. Add filler sentences only if under _SOFT_WORD_LIMIT
      8. Never return fewer than _MIN_WORDS -- fall back to longer version

    Never raises. Returns original text on any unhandled error.
    """
    if not text or not text.strip():
        return ""

    try:
        # Step 1: split into sentences
        raw_sentences = re.split(r"(?<=[.!?])\s+", text.strip())

        # Step 2: drop boilerplate sentences and very short fragments
        kept: list[str] = []
        for s in raw_sentences:
            s = s.strip()
            if len(s) < 20:
                continue  # too short -- likely a URL fragment or stray punctuation
            if _SENT_NOISE.match(s):
                continue  # boilerplate sentence
            kept.append(s)

        if not kept:
            return text  # everything got stripped -- return original

        # Step 3: deduplicate near-identical sentences (> 75% shared 4+ char words)
        unique: list[str] = []
        seen_fps: list[set[str]] = []
        for sent in kept:
            words_set = set(re.findall(r"\b\w{4,}\b", sent.lower()))
            is_dup = False
            for fp in seen_fps:
                if fp and words_set:
                    overlap = len(words_set & fp) / max(len(words_set), len(fp))
                    if overlap >= 0.75:
                        is_dup = True
                        break
            if not is_dup:
                unique.append(sent)
                seen_fps.append(words_set)

        if not unique:
            return text

        # Step 4: short after cleanup -- return as-is
        cleaned = " ".join(unique)
        if len(cleaned.split()) <= 150:
            return cleaned

        # Step 5: deduplication alone enough?
        if len(cleaned.split()) <= _SOFT_WORD_LIMIT:
            return cleaned

        # Step 6: still over limit -- preserve facts, trim fillers
        order = {s: i for i, s in enumerate(unique)}
        fact_sents:   list[str] = []
        filler_sents: list[str] = []
        for sent in unique:
            if _FILLER_SENT.match(sent) and not _is_fact_bearing(sent):
                filler_sents.append(sent)
            else:
                fact_sents.append(sent)

        result: list[str] = list(fact_sents)
        wc = sum(len(s.split()) for s in result)
        for sent in filler_sents:
            if wc + len(sent.split()) <= _SOFT_WORD_LIMIT:
                result.append(sent)
                wc += len(sent.split())

        # Restore original narrative order
        result.sort(key=lambda s: order.get(s, 999))
        compressed = " ".join(result)

        # Step 7: safety -- never return less than _MIN_WORDS
        if len(compressed.split()) < _MIN_WORDS:
            return cleaned  # cleaned is longer; still better than raw

        return compressed

    except Exception:  # noqa: BLE001
        return text  # never corrupt the pipeline


def _build_prompt(story: Story, retry_hint: str | None = None) -> str:
    """Build the user-facing prompt with structured story facts.

    Args:
        story: The story to generate a post for.
        retry_hint: If this is a retry, the reason the previous attempt failed.
            Appended to the prompt so the LLM knows what to fix.
    """
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

    prompt = f"""Write a Facebook post about the following news story for Global Pulse News.

TITLE: {story.title}
SOURCE: {story.source_name}
PUBLISHED: {published}
SUMMARY: {_compress_summary(story.raw_summary) if story.raw_summary else "No summary available."}
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
Then add one empty line before the body.
BODY: 2-3 short paragraphs (max 2-3 sentences each), with one empty line between paragraphs.
OPTIONAL QUESTION: Include only a specific, natural question that adds civic or practical context. Omit it when it would be generic. Never request likes, shares, comments, tags, or reactions, and do not add 👇.
SOURCES: 📰 Sources: {', '.join(unique_source_names)}

Write the post now."""
    if retry_hint:
        prompt += (
            f"\n\nIMPORTANT: Your previous attempt failed because: {retry_hint}. "
            "Fix this specific issue in your response."
        )
    return prompt


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
    post = sanitize_facebook_caption(post)
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
    return sanitize_facebook_caption(opening + (f"\n\n{body}" if body else ""))


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
    # Trim to max 10 words (safer than 45-char hard truncation mid-word)
    _incomplete = {
        "IN", "ON", "AT", "OF", "TO", "THE", "A", "AN", "AND", "OR", "BUT",
        "FOR", "WITH", "AS", "BY", "FROM", "STILL", "INTO", "OVER", "AFTER",
        "AMID", "THAT", "THIS", "ITS", "US", "IT", "HE", "SHE", "THEY",
        "MISSILE", "MISSILES", "WORLD", "CAPITAL", "CANYON", "PLAN",
        "REPORT", "REPORTS", "MOVE", "STEP", "SAYS", "SAID",
        "STATUS", "ISSUES", "MATTER", "SITUATION",
    }
    words = title.split()[:10]
    while words and words[-1].upper().strip(".,") in _incomplete:
        words.pop()
    return " ".join(words).upper() if words else title.upper()


def _parse_card_headline(raw: str) -> str | None:
    """Extract and clean the CARD_HEADLINE line from LLM output."""
    m = re.search(r"CARD_HEADLINE:\s*\n(.+)", raw, re.IGNORECASE)
    if not m:
        return None
    headline = m.group(1).strip().strip('"').strip("'")

    # Strip label prefixes (FACT-LED:, BREAKING:, XENOPHOBIA:, etc.)
    headline = re.sub(
        r"^(?:BREAKING|JUST IN|UPDATE|WATCH|EXCLUSIVE|FACT-LED|FACT LED"
        r"|DEVELOPING|ALERT|ANALYSIS|OPINION|EXPLAINER"
        r"|[A-Z][A-Z\s]{2,20})\s*:\s*",
        "", headline, flags=re.IGNORECASE
    ).strip()

    # Remove weak filler openers
    headline = re.sub(
        r"^(ALMOST|NEARLY|ABOUT|ROUGHLY|OVER|MORE THAN|UP TO|AT LEAST)\s+",
        "", headline, flags=re.IGNORECASE
    ).strip()

    # Trim to max 20 words (replaces hard 45-char truncation)
    _incomplete = {
        "IN", "ON", "AT", "OF", "TO", "THE", "A", "AN", "AND", "OR", "BUT",
        "FOR", "WITH", "AS", "BY", "FROM", "STILL", "INTO", "OVER", "AFTER",
        "AMID", "THAT", "THIS", "ITS", "US", "IT", "HE", "SHE", "THEY",
        "MISSILE", "MISSILES", "WORLD", "CAPITAL", "CANYON", "PLAN",
        "REPORT", "REPORTS", "MOVE", "STEP", "SAYS", "SAID",
        "STATUS", "ISSUES", "MATTER", "SITUATION",
    }
    words = headline.split()[:20]
    while words and words[-1].upper().strip(".,") in _incomplete:
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
# ---------------------------------------------------------------------------
# LLM hashtag parsing + validation
# ---------------------------------------------------------------------------

# Tags that are too generic, invented, or irrelevant to block from LLM output
_BLOCKED_HASHTAGS = {
    "#news", "#update", "#latest", "#breaking", "#trending", "#viral",
    "#worldnews", "#globalnews", "#internationalnews", "#today", "#follow",
    "#like", "#share", "#repost", "#follow", "#worldwide", "#global",
    "#breakingnews", "#breakingupdate", "#newsstory", "#newsupdate",
    "#globalpulsenews",  # added automatically, never from LLM
}


def _parse_llm_hashtags(raw_output: str) -> list[str]:
    """Extract and validate hashtags from the LLM HASHTAGS: block.

    Returns a list of clean, validated hashtag strings.
    Returns [] if the block is missing or all tags are invalid.
    Never raises.
    """
    try:
        # Find the HASHTAGS: block
        m = re.search(r"HASHTAGS:\s*\n(.+)", raw_output, re.IGNORECASE)
        if not m:
            return []

        line = m.group(1).strip()
        # Extract all #word tokens from the line
        candidates = re.findall(r"#[A-Za-z][A-Za-z0-9_]{1,29}", line)

        valid: list[str] = []
        for tag in candidates:
            lower = tag.lower()
            # Block generic/banned tags
            if lower in _BLOCKED_HASHTAGS:
                continue
            # Block invented compound tags (2+ caps in long tag = likely invented)
            inner = tag[1:]  # strip the #
            upper_count = sum(1 for c in inner if c.isupper())
            _known_twoword = {
                "saudiarabia", "celinedion", "northkorea", "southkorea",
                "southafrica", "middleeast", "unitednations", "elonmusk",
                "worldbank", "federalreserve", "wallstreet", "newzealand",
                "srilanka", "humanrights", "climatechange", "naturaldisaster",
                "stockmarket", "whitehouse", "stiffpersonsyndrome",
            }
            if upper_count >= 2 and len(inner) > 7 and inner.lower() not in _known_twoword:
                continue
            if tag not in valid:
                valid.append(tag)
            if len(valid) >= 4:
                break

        return valid
    except Exception:
        return []


# Deterministic hashtag generation — always 3–5 relevant tags
# ---------------------------------------------------------------------------

# Category → base hashtags (always included for that category)
_CATEGORY_HASHTAGS: dict[str, list[str]] = {
    "breaking":      ["#BreakingNews", "#WorldNews"],
    "war":           ["#War", "#Conflict"],
    "world":         ["#WorldNews", "#International"],
    "politics":      ["#Politics", "#Government", "#Policy", "#Democracy"],
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
    (r"(?<![a-z])WHO(?![a-z])|world health organization",  "#WHO"),
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

    # ── Step 1: keyword-matched story-specific tags (title-first strategy) ─────
    # Tags matched in the TITLE score higher than summary-only matches.
    # This prevents summary bleed-over: e.g. '#France' on a Democrats story
    # where France only appears in a stray unrelated summary sentence.
    text_original  = ((story.title or "") + " " + (story.raw_summary or ""))
    title_original = (story.title or "")
    title_lower    = title_original.lower()

    # Collect candidates split by relevance tier
    title_tags:   list[str] = []  # keyword found in title
    summary_tags: list[str] = []  # keyword found in summary only
    for pattern, tag in _KEYWORD_HASHTAGS:
        if tag in tags:
            continue
        if "(?<!" in pattern:
            # Case-sensitive pattern: match against original-case strings
            in_title   = bool(re.search(pattern, title_original, 0))
            in_summary = bool(re.search(pattern, text_original, 0))
        else:
            in_title   = bool(re.search(pattern, title_lower, re.IGNORECASE))
            in_summary = bool(re.search(pattern, text, re.IGNORECASE))
        if in_title:
            title_tags.append(tag)
        elif in_summary:
            summary_tags.append(tag)

    # Add ONLY title-matched tags here (most relevant)
    # summary_tags are added later in Step 3b as last resort
    for tag in title_tags:
        if len(tags) >= 5:
            break
        if tag not in tags:
            tags.append(tag)

    # ── Step 2: named entity extraction removed ──────────────────
    # Produced invented tags like #MuslimDemocrats, #UsCongress.
    # LLM now handles story-specific tags via _parse_llm_hashtags().

    # ── Step 3: category base tag (runs BEFORE summary-only tags) ───────
    # Category tags are more relevant than summary-only keyword matches.
    if len(tags) < 4:
        for tag in _CATEGORY_HASHTAGS.get(category, ["#WorldNews"]):
            if len(tags) >= 4:
                break
            if tag not in tags:
                tags.append(tag)

    # ── Step 3b: summary-only keyword tags (last resort to reach min 3) ───
    # Added last -- prevents summary bleed-over on multi-story summaries.
    for tag in summary_tags:
        if len(tags) >= 5:
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
