"""Classify topics from verified claims, with the verified category as a prior."""

from __future__ import annotations

import re

from distribution.models import DistributionStory, TopicResult


TOPICS = {"breaking", "tech", "business", "politics", "science", "sports", "trending", "entertainment", "jobs", "world", "crime", "climate", "war", "wellness"}
_ALIASES = {"technology": "tech", "health": "wellness", "news": "world"}
_TERMS = {
    "tech": ("technology", "artificial intelligence", "software", "cyber", "chip"),
    "business": ("business", "economy", "market", "trade", "company", "interest rate"),
    "politics": ("election", "government", "president", "congress", "senate", "policy"),
    "science": ("research", "scientist", "space", "discovery", "study"),
    "sports": ("football", "cricket", "basketball", "tennis", "tournament"),
    "entertainment": ("film", "actor", "music", "singer", "television"),
    "jobs": ("jobs", "hiring", "employment", "career", "workforce"),
    "crime": ("police", "court", "arrest", "murder", "charged", "shooting"),
    "climate": ("hurricane", "storm", "flood", "wildfire", "climate", "earthquake"),
    "war": ("war", "missile", "airstrike", "troops", "invasion", "ceasefire"),
    "wellness": ("health", "hospital", "fitness", "nutrition", "wellness"),
}


def classify_topic(story: DistributionStory) -> TopicResult:
    category = _ALIASES.get(story.category.lower(), story.category.lower())
    scores = {topic: (3 if topic == category else 0) for topic in TOPICS}
    text = " ".join(story.verified_claims).lower()
    evidence: dict[str, list[str]] = {topic: [] for topic in TOPICS}
    for topic, terms in _TERMS.items():
        for term in terms:
            if re.search(rf"\b{re.escape(term)}\w*\b", text):
                scores[topic] += 2
                evidence[topic].append(term)
    ranked = sorted(scores, key=lambda key: (-scores[key], key))
    if scores[ranked[0]] <= 0:
        return TopicResult(None, [], 0.0, [])
    primary = ranked[0]
    secondary = [topic for topic in ranked[1:] if scores[topic] >= 2][:3]
    confidence = min(0.98, 0.58 + scores[primary] * 0.07)
    reasons = ([f"verified category: {category}"] if primary == category else []) + evidence[primary]
    return TopicResult(primary, secondary, round(confidence, 2), reasons)
