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

    story.post_content = post_content
    story.hashtags = hashtags
    return story


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a professional social media editor for a worldwide news Facebook Page called "World Update".

Your job is to write clear, factual, engaging Facebook posts based on verified news facts.

Rules you must always follow:
- Never copy article text word-for-word. Write in your own words.
- Use plain language suitable for a general worldwide audience.
- Clearly label unconfirmed information with words like "reportedly", "according to sources", or "it is alleged".
- Do not sensationalise or use clickbait headlines.
- Do not include personal opinions or commentary.
- The post must feel informative and trustworthy.

At the very end of the post, always include a sources line in this exact format:
📰 Sources: <comma-separated source names>

For hashtags:
- Include 6-10 hashtags that are SPECIFIC to the story (people, places, events, topics involved).
- Always include #WorldUpdate and #BreakingNews.
- Use hashtags that people actually search for (e.g. #Nepal, #Flood, #DisasterRelief not just #News).
- Mix broad tags (#WorldNews) with specific ones (#NepalFlood2026).

Output format (return exactly this structure, nothing else):
POST:
<your full Facebook post text here>

HASHTAGS:
<comma-separated hashtags starting with #>
"""


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

    return f"""Write a Facebook post about the following news story.

TITLE: {story.title}
SOURCE: {story.source_name}
PUBLISHED: {published}
SUMMARY: {story.raw_summary or "No summary available."}
VERIFICATION: {verification_note}{corroborating_text}
ALL SOURCES TO CITE: {', '.join(unique_source_names)}

The post must contain:
1. A strong, factual opening line (not clickbait)
2. A concise summary (2-4 sentences)
3. The key facts
4. Context if needed
5. A short closing statement
6. A sources line at the very end: 📰 Sources: {', '.join(unique_source_names)}

For the HASHTAGS section, generate 6-10 hashtags that are:
- Specific to the people, places, organisations, and events in THIS story
- Mix of broad (#WorldNews) and specific (#NepalFlood2026, #Zelenskyy, #Ukraine)
- Always include #WorldUpdate and #BreakingNews
- Tags that people actually search for on Facebook

Write the post now."""


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
