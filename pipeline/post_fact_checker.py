"""Deterministic grounding checks for generated Facebook captions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from models import Story


_NUMBER = re.compile(r"(?<!\w)(?:[$£€])?\d[\d,]*(?:\.\d+)?%?(?!\w)")
_RELATION = re.compile(
    r"\b(to|from|in|near|across|inside|outside)\s+(?:the\s+)?([A-Z][A-Za-z.-]+(?:\s+[A-Z][A-Za-z.-]+){0,2})"
)
_WORDS = re.compile(r"\b[A-Za-z][A-Za-z'-]{3,}\b")
_STOP = {
    "about", "after", "before", "could", "first", "from", "have", "into",
    "more", "news", "only", "over", "says", "source", "sources", "that",
    "their", "these", "they", "this", "what", "when", "where", "which",
    "while", "will", "with", "would", "globalpulsenews", "worldnews",
}


@dataclass
class FactCheckResult:
    passed: bool
    issues: list[str] = field(default_factory=list)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%$]+", " ", text.lower())).strip()


def _evidence_parts(story: Story) -> list[str]:
    parts = [story.title, story.raw_summary or "", story.article_text or ""]
    for source in story.corroborating_sources or []:
        parts.extend([
            source.get("title", ""), source.get("summary", ""),
            source.get("article_text", ""), source.get("description", ""),
        ])
    return [part for part in parts if part]


def check_generated_facts(story: Story, post: str) -> FactCheckResult:
    """Reject captions containing unsupported numbers, locations, or claim sentences."""
    evidence_parts = _evidence_parts(story)
    evidence = " ".join(evidence_parts)
    evidence_norm = _normalise(evidence)
    issues: list[str] = []

    evidence_numbers = {_normalise(value) for value in _NUMBER.findall(evidence)}
    for value in _NUMBER.findall(post):
        normal = _normalise(value)
        # Decorative list numbering and the page name are not factual claims.
        if normal and normal not in evidence_numbers:
            issues.append(f"unsupported number: {value}")

    for sentence in re.split(r"(?<=[.!?])\s+|\n+", post):
        sentence = sentence.strip()
        if not sentence or sentence.endswith("?") or sentence.lower().startswith(("sources:", "📰 sources:")):
            continue

        for prep, place in _RELATION.findall(sentence):
            relation = _normalise(f"{prep} {place}")
            place_norm = _normalise(place)
            # Prepositions often change in faithful paraphrases (for example,
            # "wanted in the UK" becoming "returned to the UK"). Treat the
            # location as grounded when the place itself exists in evidence;
            # sentence-level overlap still checks the surrounding claim.
            if relation and place_norm not in evidence_norm:
                issues.append(f"unsupported location relation: {prep} {place}")

        words = {w.lower() for w in _WORDS.findall(sentence) if w.lower() not in _STOP}
        if len(words) >= 5:
            evidence_words = {w.lower() for w in _WORDS.findall(evidence) if w.lower() not in _STOP}
            overlap = len(words & evidence_words) / len(words)
            if overlap < 0.35:
                issues.append(f"weakly grounded sentence: {sentence[:90]}")

    return FactCheckResult(passed=not issues, issues=list(dict.fromkeys(issues)))
