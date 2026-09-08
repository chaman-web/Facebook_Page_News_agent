"""Select a concise, grounded description for the image card."""

from __future__ import annotations

import re

from models import Story
from pipeline.post_fact_checker import check_generated_facts


_STOP = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "of",
    "on", "the", "to", "with", "set", "says", "new",
}


def select_card_description(story: Story, raw_output: str) -> str | None:
    """Return a relevant description that adds information beyond the title."""
    match = re.search(
        r"CARD_DESCRIPTION:\s*\n?(.+?)(?=\n(?:POST|CARD_HEADLINES|HASHTAGS):|\Z)",
        raw_output,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    description = re.sub(r"\s+", " ", match.group(1)).strip().strip('"\'')
    description = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", description).strip()
    if description.endswith("?") or re.search(r"\b(shocking|you won't believe|must see)\b", description, re.I):
        return None

    words = description.split()
    if not 7 <= len(words) <= 18:
        return None

    title_terms = _terms(story.title)
    description_terms = _terms(description)
    shared = title_terms & description_terms
    if not shared:
        return None
    similarity = len(shared) / max(1, min(len(title_terms), len(description_terms)))
    if similarity >= 0.65:
        return None

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
