"""Infer the most relevant image-card topic from verified story content."""

from __future__ import annotations

import re

from models import Story


_TOPIC_TERMS = {
    "crime": (
        "murder", "homicide", "police", "court", "trial", "arrest", "charged",
        "charges", "prosecutor", "extradition", "shooting", "fraud", "prison",
    ),
    "war": (
        "war", "airstrike", "missile", "military", "troops", "ceasefire",
        "invasion", "battle", "armed conflict", "drone strike", "hostage",
    ),
    "climate": (
        "climate", "hurricane", "cyclone", "typhoon", "wildfire", "flood",
        "earthquake", "storm", "drought", "emissions", "environment",
    ),
    "business": (
        "tariff", "trade ban", "economy", "economic", "market", "inflation",
        "interest rate", "company", "business", "stock", "oil price", "layoff",
    ),
    "politics": (
        "election", "government", "parliament", "minister", "president",
        "prime minister", "sanction", "diplomatic", "policy", "legislation",
        "senate", "congress", "political",
    ),
    "technology": (
        "technology", "artificial intelligence", " ai ", "software", "cyber",
        "smartphone", "chip", "robot", "app", "internet", "data breach",
    ),
    "science": (
        "research", "scientist", "study", "space", "nasa", "discovery",
        "vaccine", "disease", "hospital", "medical", "medicine",
    ),
    "wellness": (
        "wellness", "mental health", "fitness", "nutrition", "diet", "exercise",
        "healthy", "wellbeing",
    ),
    "sports": (
        "football", "cricket", "basketball", "tennis", "champions league",
        "tournament", "match", "goal", "coach", "athlete", "world cup",
    ),
    "entertainment": (
        "film", "movie", "music", "actor", "actress", "singer", "celebrity",
        "documentary", "festival", "album", "television", "netflix",
    ),
    "jobs": (
        "jobs", "hiring", "employment", "career", "workforce", "recruitment",
        "jobless", "unemployment",
    ),
}


def card_topic(story: Story) -> str:
    """Return a content-derived topic for the card's top label."""
    title = f" {story.title.lower()} "
    evidence = " ".join((story.raw_summary or "", story.article_text or "")[:2]).lower()
    scores: dict[str, int] = {}
    for topic, terms in _TOPIC_TERMS.items():
        score = 0
        for term in terms:
            pattern = _term_pattern(term)
            score += len(pattern.findall(title)) * 4
            score += min(len(pattern.findall(evidence)), 2)
        scores[topic] = score

    best_topic, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score >= 4:
        return best_topic

    current = (story.category or "").lower()
    if current in _TOPIC_TERMS:
        return current
    if current == "breaking":
        return "breaking"
    if current == "world":
        return "world"
    return "news"


def _term_pattern(term: str) -> re.Pattern[str]:
    term = term.strip()
    if " " in term:
        return re.compile(rf"\b{re.escape(term)}\b", re.I)
    # Basic suffix support covers sanctions/sanctioned, arrests/arrested, etc.
    stem = term[:-2] if term.endswith("ed") and len(term) > 5 else term
    return re.compile(rf"\b{re.escape(stem)}(?:s|es|ed|ing)?\b", re.I)
