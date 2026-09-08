"""Compare core factual claims across independent reports of one event."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


_STOPWORDS = {
    "about", "after", "again", "against", "amid", "been", "before", "being",
    "could", "from", "have", "into", "latest", "more", "news", "over", "report",
    "reports", "said", "says", "that", "their", "there", "these", "they", "this",
    "those", "through", "under", "where", "which", "while", "will", "with", "would",
}

_OUTCOME_CONTRADICTIONS = [
    (r"\b(?:wins?|won|victory)\b", r"\b(?:loses?|lost|defeat)\b"),
    (r"\b(?:approved?|passes?|passed|agreed?)\b", r"\b(?:rejects?|rejected|blocked?)\b"),
    (r"\b(?:alive|survives?|survived)\b", r"\b(?:dead|dies?|died|killed)\b"),
    (r"\b(?:released?|freed)\b", r"\b(?:arrested?|detained?|jailed)\b"),
    (r"\b(?:ceasefire|truce)\b", r"\b(?:offensive|attack launched|fighting resumes?)\b"),
    (r"\b(?:confirmed?|admits?)\b", r"\b(?:denies?|denied|unconfirmed)\b"),
]

_COUNT_PATTERN = re.compile(
    r"\b(?P<count>\d[\d,]*)\s+(?:(?:people|persons?|civilians?|soldiers?|workers?|children)\s+)?"
    r"(?P<kind>killed|dead|died|injured|wounded|missing|arrested|detained)\b",
    re.IGNORECASE,
)


@dataclass
class ClaimComparison:
    agreement: float
    matched: bool
    contradictions: list[str] = field(default_factory=list)
    syndicated_copy: bool = False


def _keywords(text: str) -> set[str]:
    return {
        word.lower()
        for word in re.findall(r"\b[A-Za-z][A-Za-z'-]{3,}\b", text or "")
        if word.lower() not in _STOPWORDS
    }


def _critical_counts(text: str) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for match in _COUNT_PATTERN.finditer(text or ""):
        count = int(match.group("count").replace(",", ""))
        kind = match.group("kind").lower()
        if kind in {"dead", "died"}:
            kind = "killed"
        result.setdefault(kind, set()).add(count)
    return result


def _wire_origin(text: str, domain: str = "") -> str:
    domain = (domain or "").lower()
    for fragment, origin in (
        ("reuters", "reuters"), ("apnews", "associated-press"),
        ("associatedpress", "associated-press"), ("afp", "afp"),
    ):
        if fragment in domain:
            return origin
    lead = (text or "")[:400]
    patterns = (
        (r"^\s*(?:\([^)]*\)\s*)?reuters\s*[-—:]", "reuters"),
        (r"\bby\s+.{0,100}\breuters\b", "reuters"),
        (r"\b(?:reporting by|source:|©)\s*reuters\b", "reuters"),
        (r"^\s*(?:the\s+)?associated press\s*[-—:]", "associated-press"),
        (r"\b(?:by|source:|©)\s+(?:the\s+)?associated press\b", "associated-press"),
        (r"^\s*afp\s*[-—:]|\b(?:by|source:|©)\s+afp\b", "afp"),
    )
    for pattern, origin in patterns:
        if re.search(pattern, lead, re.IGNORECASE):
            return origin
    return ""


def compare_reports(
    primary_title: str,
    primary_text: str,
    other_title: str,
    other_text: str,
    primary_domain: str = "",
    other_domain: str = "",
) -> ClaimComparison:
    """Measure event agreement and detect a small set of material contradictions."""
    title_a = (primary_title or "").lower()
    title_b = (other_title or "").lower()
    # Compare the headline and lead material. Full articles often mention old
    # outcomes or earlier casualty figures later in the page, which should not
    # be mistaken for a contradiction in the current core claim.
    text_a = f"{primary_title} {(primary_text or '')[:1600]}".strip()
    text_b = f"{other_title} {(other_text or '')[:1600]}".strip()

    contradictions: list[str] = []
    for positive, negative in _OUTCOME_CONTRADICTIONS:
        if (re.search(positive, text_a, re.IGNORECASE) and re.search(negative, text_b, re.IGNORECASE)) or (
            re.search(negative, text_a, re.IGNORECASE) and re.search(positive, text_b, re.IGNORECASE)
        ):
            contradictions.append("conflicting outcome language")
            break

    counts_a = _critical_counts(text_a)
    counts_b = _critical_counts(text_b)
    for kind in counts_a.keys() & counts_b.keys():
        if counts_a[kind].isdisjoint(counts_b[kind]):
            contradictions.append(
                f"conflicting {kind} counts: {sorted(counts_a[kind])} vs {sorted(counts_b[kind])}"
            )

    words_a = _keywords(text_a)
    words_b = _keywords(text_b)
    overlap = len(words_a & words_b) / max(min(len(words_a), len(words_b)), 1)
    title_similarity = SequenceMatcher(None, title_a, title_b).ratio()
    agreement = round(min(1.0, title_similarity * 0.55 + overlap * 0.45), 3)

    norm_a = " ".join((primary_text or "").lower().split())
    norm_b = " ".join((other_text or "").lower().split())
    same_wire_origin = bool(
        _wire_origin(primary_text, primary_domain)
        and _wire_origin(primary_text, primary_domain) == _wire_origin(other_text, other_domain)
        and primary_domain != other_domain
    )
    syndicated = same_wire_origin or (
        len(norm_a) >= 180
        and len(norm_b) >= 180
        and SequenceMatcher(None, norm_a[:1500], norm_b[:1500]).ratio() >= 0.94
    )

    return ClaimComparison(
        agreement=agreement,
        matched=agreement >= 0.35 and not contradictions,
        contradictions=contradictions,
        syndicated_copy=syndicated,
    )
