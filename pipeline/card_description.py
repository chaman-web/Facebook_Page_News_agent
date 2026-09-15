"""Select a concise, grounded, engaging description for the image card."""

from __future__ import annotations

import re

from models import Story
from pipeline.post_fact_checker import check_generated_facts


_STOP = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "of",
    "on", "the", "to", "with", "set", "says", "new",
}

# Vague speculation patterns — no concrete anchor means the reader gains nothing.
_VAGUE_HEDGE = re.compile(
    r"^[^.!]*\b(could|may|might|would)\b[^.!]*"
    r"(?:face|see|lead|result|cause|create|bring|pose|mean)"
    r"[^.!]*\b(challenge|difficult|uncertain|issue|concern|problem|impact|effect|pressure)\b",
    re.IGNORECASE,
)

# A "concrete anchor" requires one of:
#   - a digit sequence
#   - a proper noun (capitalised word mid-sentence)
#   - a quoted phrase (any quote style)
_NUMBER = re.compile(r"\d")

# Build quote set as a character class string to avoid encoding issues
_QUOTE_CHARS = "\"'\u2018\u2019\u201c\u201d"
_QUOTED = re.compile(
    "[" + re.escape(_QUOTE_CHARS) + "].{3,}[" + re.escape(_QUOTE_CHARS) + "]",
    re.UNICODE,
)


def _has_concrete_anchor(description: str) -> bool:
    """Return True if the description contains a number, proper noun, or quote."""
    if _NUMBER.search(description):
        return True
    if _QUOTED.search(description):
        return True
    # Check for a capitalised word that is NOT the first word
    words = description.split()
    for word in words[1:]:
        clean = re.sub(r"[^A-Za-z]", "", word)
        if clean and clean[0].isupper() and len(clean) > 1:
            return True
    return False


def select_card_description(story: Story, raw_output: str) -> str | None:
    """Return a relevant, engaging description that adds information beyond the title.

    Validation pipeline (all must pass):
    1. CARD_DESCRIPTION block must exist in raw_output
    2. Cleaned text must be 7-18 words
    3. No clickbait / question endings
    4. Must share at least 2 meaningful keywords with the story title
    5. Must not be a near-paraphrase of the title (similarity < 0.85)
    6. Must contain a concrete anchor: number, proper noun, or quote
    7. Must not be pure vague speculation without a concrete anchor
    8. Must pass fact grounding check
    """
    match = re.search(
        r"CARD_DESCRIPTION:\s*\n?(.+?)(?=\n(?:POST|CARD_HEADLINES|HASHTAGS):|\Z)",
        raw_output,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    description = re.sub(r"\s+", " ", match.group(1)).strip().strip("\"'")
    description = re.sub(r"^(?:[-*\u2022]|\d+[.)])\s*", "", description).strip()

    # 3 -- clickbait / question
    if description.endswith("?") or re.search(
        r"\b(shocking|you won't believe|must see)\b", description, re.I
    ):
        return None

    # 2 -- word count
    words = description.split()
    if not 7 <= len(words) <= 18:
        return None

    # 4 & 5 -- keyword overlap
    title_terms = _terms(story.title)
    description_terms = _terms(description)
    shared = title_terms & description_terms

    # Must share at least 2 meaningful keywords with the title
    if len(shared) < 2:
        return None

    similarity = len(shared) / max(1, min(len(title_terms), len(description_terms)))
    if similarity >= 0.85:
        return None

    # 6 -- must contain a concrete anchor (number, proper noun, or quote)
    if not _has_concrete_anchor(description):
        return None

    # 7 -- reject vague speculation with no number anchor
    if _VAGUE_HEDGE.match(description) and not _NUMBER.search(description):
        return None

    # 8 -- fact grounding
    if not check_generated_facts(story, description).passed:
        return None

    return description.rstrip(".! ") + "."


def _terms(text: str) -> set[str]:
    terms = set()
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if word in _STOP or len(word) <= 2:
            continue
        if len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        terms.add(word)
    return terms
