"""Select a factual, mobile-friendly, high-attention image-card headline."""

from __future__ import annotations

import re

from models import Story


_CLICKBAIT = re.compile(
    r"\b(?:you won't believe|shocking truth|what happens next|breaks the internet|"
    r"must see|jaw-dropping|unbelievable)\b", re.I
)
_ACTION = re.compile(
    r"\b(?:kills?|wins?|loses?|bans?|approves?|rejects?|launches?|strikes?|"
    r"resigns?|arrests?|orders?|warns?|cuts?|raises?|falls?|surges?|collapses?|"
    r"declares?|signs?|opens?|closes?|halts?|confirms?)\b", re.I
)
_IMPACT = re.compile(
    r"\b(?:dead|killed|war|attack|crisis|emergency|historic|record|earthquake|"
    r"flood|fire|election|ban|collapse|breakthrough|ceasefire|resigns?)\b", re.I
)
_NUMBER = re.compile(r"(?<!\w)(?:[$£€])?\d[\d,]*(?:\.\d+)?%?(?!\w)")
_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9'%-]*")
_STOP = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "to", "with", "after", "amid", "over",
}
_WEAK_END = _STOP | {"says", "said", "update", "situation", "move", "step"}


def select_card_headline(story: Story, raw_output: str) -> str:
    candidates = _extract_candidates(raw_output)
    candidates.extend(_title_candidates(story.title))
    valid = []
    for candidate in candidates:
        cleaned = _clean(candidate)
        score = _score(cleaned, story)
        if score is not None:
            valid.append((score, cleaned))
    if valid:
        return max(valid, key=lambda item: item[0])[1].upper()
    return _safe_fallback(story.title)


def _extract_candidates(raw: str) -> list[str]:
    block = re.search(r"CARD_HEADLINES?:\s*\n(.*?)(?=\n\s*POST:|\Z)", raw, re.I | re.S)
    if not block:
        return []
    result = []
    for line in block.group(1).splitlines():
        line = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", line).strip()
        if line:
            result.append(line)
    return result


def _title_candidates(title: str) -> list[str]:
    title = re.sub(r"\s+(?:[-|—–])\s+[^-|—–]+$", "", title).strip()
    parts = [title]
    parts.extend(part.strip() for part in re.split(r"\s+[—–:]\s+", title) if part.strip())
    return parts


def _clean(value: str) -> str:
    value = value.strip().strip('"\'')
    value = re.sub(r"^(?:BREAKING|JUST IN|UPDATE|WATCH|EXCLUSIVE)\s*:\s*", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" .,:;—–-")


def _score(candidate: str, story: Story) -> float | None:
    words = _WORDS.findall(candidate)
    if not 4 <= len(words) <= 10 or len(candidate) > 68:
        return None
    if candidate.endswith("?") or _CLICKBAIT.search(candidate):
        return None
    if words[-1].lower().strip(".,") in _WEAK_END:
        return None

    evidence = " ".join([
        story.title or "", story.raw_summary or "", story.article_text or "",
        *[source.get("title", "") + " " + source.get("summary", "")
          for source in story.corroborating_sources or []],
    ])
    evidence_numbers = {_normal(number) for number in _NUMBER.findall(evidence)}
    if any(_normal(number) not in evidence_numbers for number in _NUMBER.findall(candidate)):
        return None

    candidate_terms = {word.lower() for word in words if word.lower() not in _STOP and len(word) > 2}
    evidence_terms = {
        word.lower() for word in _WORDS.findall(evidence)
        if word.lower() not in _STOP and len(word) > 2
    }
    overlap = len(candidate_terms & evidence_terms) / max(len(candidate_terms), 1)
    if overlap < 0.55:
        return None

    length_score = 24 - abs(len(words) - 7) * 3
    return (
        overlap * 40
        + length_score
        + (12 if _ACTION.search(candidate) else 0)
        + min(len(_IMPACT.findall(candidate)) * 5, 10)
        + (8 if _NUMBER.search(candidate) else 0)
    )


def _safe_fallback(title: str) -> str:
    cleaned = _clean(_title_candidates(title)[0])
    words = _WORDS.findall(cleaned)
    words = words[:10]
    while words and words[-1].lower() in _WEAK_END:
        words.pop()
    return " ".join(words).upper() or "LATEST NEWS UPDATE"


def _normal(value: str) -> str:
    return value.lower().replace(",", "").strip()
