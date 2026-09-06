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
from datetime import timezone

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
    """
    from openai import OpenAIError

    prompt = _build_prompt(story)
    model = config.OLLAMA_MODEL if config.LLM_BACKEND == "ollama" else config.OPENAI_MODEL

    logger.info(
        "Generating post with %s model '%s' for: %s",
        config.LLM_BACKEND.upper(),
        model,
        story.title,
    )

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

    post_content, hashtags = _parse_response(raw_output)

    if not post_content:
        raise GenerationError("LLM returned an empty post body.")

    post_content = _format_post(post_content)

    story.post_content = post_content
    story.hashtags     = hashtags
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

FORMATTING RULES (follow exactly):
- Write in SHORT PARAGRAPHS — maximum 2-3 sentences per paragraph.
- Put a BLANK LINE between every paragraph.
- Start with the most important fact — the hook that stops the scroll.
- DO NOT start with boring phrases like "In a significant development..." or "According to reports...".
- The very first sentence must be punchy, direct, and make the reader want to keep reading.
- Use 1-2 relevant emojis per paragraph as visual anchors — not decoration.
- End with a direct QUESTION to the audience (e.g. "What do you think? Drop your opinion below 👇").
- Keep total post length between 150-400 words. Concise wins on Facebook.

At the very end of the post, always include:
📰 Sources: <comma-separated source names>

HASHTAGS:
- Include EXACTLY 3-5 hashtags.
- Always include #GlobalPulseNews.
- Use hashtags SPECIFIC to this story (people, places, events).
- No generic spam tags.

Output format (return exactly this structure, nothing else):
POST:
<your full Facebook post text here>

HASHTAGS:
<comma-separated hashtags starting with #>
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

    return f"""Write a Facebook post about the following news story for Global Pulse News.

TITLE: {story.title}
SOURCE: {story.source_name}
PUBLISHED: {published}
SUMMARY: {story.raw_summary or "No summary available."}
VERIFICATION: {verification_note}{corroborating_text}
ALL SOURCES TO CITE: {', '.join(unique_source_names)}
CATEGORY EMOJI: {cat_emoji} — use this emoji in your post where relevant.

The post MUST follow this exact structure:
1. HOOK — first sentence: the most striking/important fact. Direct, punchy, no boring openers.
2. BODY — 2-3 short paragraphs (max 2-3 sentences each), blank line between each.
3. CONTEXT — one short paragraph with background if relevant.
4. CLOSING QUESTION — a direct question to the audience + 👇 emoji.
5. SOURCES LINE — last line: 📰 Sources: {', '.join(unique_source_names)}

HASHTAGS: generate EXACTLY 3-5 hashtags. Always include #GlobalPulseNews. Make them specific to this story.

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

def _format_post(post: str) -> str:
    """
    1. Ensure blank lines between paragraphs (max 3 sentences per paragraph).
    2. Ensure the post ends with an engagement question.
    """
    import random

    # --- Paragraph splitting ---
    # Collapse multiple blank lines → single blank line
    post = re.sub(r"\n{3,}", "\n\n", post.strip())

    # Split into existing paragraphs
    paragraphs = [p.strip() for p in post.split("\n\n") if p.strip()]

    # Split any paragraph with more than 3 sentences into smaller chunks
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
    # Check if any of the last 2 paragraphs already has a question
    tail = " ".join(split_paragraphs[-2:]) if len(split_paragraphs) >= 2 else last
    has_question = "?" in tail or "👇" in tail
    if not has_question:
        split_paragraphs.append(random.choice(_QUESTION_FALLBACKS))

    return "\n\n".join(split_paragraphs)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> tuple[str, list[str]]:
    """Extract post content and hashtags from the LLM response."""
    post_content = ""
    hashtags: list[str] = []

    # Extract POST section
    post_match = re.search(r"POST:\s*\n(.*?)(?=\nHASHTAGS:|\Z)", raw, re.DOTALL | re.IGNORECASE)
    if post_match:
        post_content = post_match.group(1).strip()

    # Extract HASHTAGS section
    hashtag_match = re.search(r"HASHTAGS:\s*\n?(.*)", raw, re.DOTALL | re.IGNORECASE)
    if hashtag_match:
        raw_tags = hashtag_match.group(1).strip()
        tokens = re.split(r"[,\s]+", raw_tags)
        hashtags = [t.strip() for t in tokens if t.startswith("#")]

    # Fallback: treat whole response as post if parsing failed
    if not post_content:
        post_content = raw.strip()

    # Fallback: extract #tags from post body if none found
    if not hashtags:
        hashtags = re.findall(r"#\w+", post_content)

    return post_content, hashtags
