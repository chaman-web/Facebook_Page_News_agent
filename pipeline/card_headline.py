"""Select a factual, high-attention, complete-thought image-card headline.

Design principles
-----------------
1. Complete thought first — a headline that trails off mid-sentence is worse
   than a longer one that reads cleanly. Word count and line count are soft
   guides, not hard gates.
2. Trust the LLM — LLM-generated candidates get a score bonus over
   title-derived fallbacks. The factual grounding checks still apply.
3. Visual fit is a tiebreaker — layout balance informs the score but no
   longer hard-rejects a good headline. 2–4 lines are all acceptable.
4. Factual safety is unchanged — numbers and keyword overlap requirements
   are kept exactly as before.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from models import Story


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_CLICKBAIT = re.compile(
    r"\b(?:you won't believe|shocking truth|what happens next|breaks the internet|"
    r"must see|jaw-dropping|unbelievable)\b", re.I
)
_ACTION = re.compile(
    r"\b(?:kills?|wins?|loses?|bans?|approves?|rejects?|launches?|strikes?|"
    r"resigns?|arrests?|orders?|warns?|cuts?|raises?|falls?|surges?|collapses?|"
    r"declares?|signs?|opens?|closes?|halts?|confirms?|acquitted?|sentences?|"
    r"indicts?|invades?|withdraws?|deploys?|blocks?|frees?|releases?|imposes?|"
    r"lifts?|crashes?|explodes?|shoots?|fires?|elects?|sworn?|impeach(?:es|ed)?)\b",
    re.I,
)
_IMPACT = re.compile(
    r"\b(?:dead|killed|war|attack|crisis|emergency|historic|record|earthquake|"
    r"flood|fire|election|ban|collapse|breakthrough|ceasefire|resigns?|"
    r"sanctions?|massacre|verdict|acquittal|missile|nuclear|invasion|hostage)\b",
    re.I,
)
_NUMBER = re.compile(r"(?<!\w)(?:[$£€])?\d[\d,]*(?:\.\d+)?%?(?!\w)")
_WORDS = re.compile(r"[A-Za-z0-9][A-Za-z0-9'%-]*")

# Words that make a headline feel incomplete when they're the last word
_STOP = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "to", "with", "after", "amid", "over",
    "its", "their", "his", "her", "our", "your",
}
_INCOMPLETE_ENDINGS = _STOP | {
    # Generic words that trail off without context
    "says", "said", "update", "situation", "move", "step",
    # Subject nouns that need a predicate to make sense
    "missile", "missiles", "canyon", "world", "capital", "plan",
    "report", "reports", "issue", "issues", "matter", "matters",
    "case", "cases", "story", "thing", "things",
    # Short ambiguous words that look like truncation
    "us", "it", "he", "she", "we", "they",
}

# Label prefixes to strip before scoring (e.g. "FACT-LED:", "ACTION-LED:", "XENOPHOBIA:")
_LABEL_PREFIX = re.compile(
    r"^(?:BREAKING|JUST IN|UPDATE|WATCH|EXCLUSIVE|"
    r"FACT-LED|FACT LED|IMPACT-LED|ACTION-LED|"
    r"DEVELOPING|ALERT|ANALYSIS|OPINION|EXPLAINER|"
    r"[A-Z][A-Z\s-]{2,20})\s*:\s*",
    re.I,
)

# Font config
_NORMAL_FONT_SIZE = 110
_CARD_TEXT_WIDTH = 1200 - 72 - 72
_HEADLINE_FONT = (
    Path(__file__).parent.parent / "assets" / "fonts" / "BarlowCondensed-ExtraBold.ttf"
)

# Score bonus for LLM-generated candidates vs title-derived fallbacks
_LLM_CANDIDATE_BONUS = 10


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def select_card_headline(story: Story, raw_output: str) -> str:
    """Return the best card headline in UPPERCASE.

    LLM-generated candidates are tried first and receive a score bonus.
    Title-derived candidates are used when no valid LLM candidate exists.
    Falls back to a trimmed story title when all scoring fails.
    """
    llm_candidates = _extract_candidates(raw_output)
    title_candidates = _title_candidates(story.title)

    valid: list[tuple[float, str]] = []

    for candidate in llm_candidates:
        cleaned = _clean(candidate)
        score = _score(cleaned, story)
        if score is not None:
            valid.append((score + _LLM_CANDIDATE_BONUS, cleaned))

    for candidate in title_candidates:
        cleaned = _clean(candidate)
        score = _score(cleaned, story)
        if score is not None:
            valid.append((score, cleaned))

    if valid:
        return max(valid, key=lambda item: item[0])[1].upper()
    return _safe_fallback(story.title)


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------

def _extract_candidates(raw: str) -> list[str]:
    """Pull all lines from the CARD_HEADLINES block in the LLM output."""
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
    """Derive headline candidates from the article title."""
    # Strip source attribution suffix (e.g. "— BBC News" at the end)
    title = re.sub(r"\s+(?:[-|—–])\s+[^-|—–]+$", "", title).strip()
    parts = [title]
    parts.extend(part.strip() for part in re.split(r"\s+[—–:]\s+", title) if part.strip())
    return parts


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def _clean(value: str) -> str:
    """Normalise whitespace, strip quotes and label prefixes."""
    value = value.strip().strip('"\'')
    # Strip label prefixes like "FACT-LED:", "BREAKING:", "XENOPHOBIA:", etc.
    value = _LABEL_PREFIX.sub("", value)
    return re.sub(r"\s+", " ", value).strip(" .,:;—–-")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(candidate: str, story: Story) -> float | None:
    """Return a numeric score or None if the candidate should be rejected.

    Hard rejects (return None):
      - Fewer than 5 words
      - More than 20 words
      - Ends with a question mark
      - Contains clickbait phrases
      - Last word is an incomplete-sounding word
      - Contains numbers not grounded in the story evidence
      - Keyword overlap with story evidence < 55%

    Soft scoring (higher is better):
      - Keyword overlap with evidence (0–40 pts)
      - Length sweet spot 5–12 words (24 pts), 13–16 (18 pts), 17–20 (10 pts)
      - Contains a strong action verb (12 pts)
      - Action verb in the last 3 words — complete thought bonus (8 pts)
      - Contains impact/consequence word (up to 10 pts)
      - Contains a number (8 pts)
      - Visual layout bonus — 2–4 lines, balanced (0–12 pts, tiebreaker only)
    """
    words = _WORDS.findall(candidate)

    # Hard word-count gates — relaxed from old 5–16
    if not 5 <= len(words) <= 20:
        return None

    # Reject questions and clickbait
    if candidate.endswith("?") or _CLICKBAIT.search(candidate):
        return None

    # Reject headlines that trail off on an incomplete-sounding last word
    last_word = words[-1].lower().strip(".,\"'")
    if last_word in _INCOMPLETE_ENDINGS:
        return None

    # Factual grounding: numbers in headline must appear in story evidence
    evidence = " ".join([
        story.title or "",
        story.raw_summary or "",
        story.article_text or "",
        *[
            source.get("title", "") + " " + source.get("summary", "")
            for source in story.corroborating_sources or []
        ],
    ])
    evidence_numbers = {_normal(n) for n in _NUMBER.findall(evidence)}
    if any(_normal(n) not in evidence_numbers for n in _NUMBER.findall(candidate)):
        return None

    # Keyword overlap with evidence
    candidate_terms = {
        w.lower() for w in words if w.lower() not in _STOP and len(w) > 2
    }
    evidence_terms = {
        w.lower() for w in _WORDS.findall(evidence)
        if w.lower() not in _STOP and len(w) > 2
    }
    overlap = len(candidate_terms & evidence_terms) / max(len(candidate_terms), 1)
    if overlap < 0.55:
        return None

    # Length score
    if 5 <= len(words) <= 12:
        length_score = 24
    elif len(words) <= 16:
        length_score = 18
    else:                       # 17–20 words
        length_score = 10

    # Action verb bonus
    has_action = bool(_ACTION.search(candidate))
    action_score = 12 if has_action else 0

    # Complete-thought bonus: action verb in last 3 words
    last_3 = " ".join(words[-3:])
    closing_verb_bonus = 8 if has_action and _ACTION.search(last_3) else 0

    # Impact word bonus
    impact_score = min(len(_IMPACT.findall(candidate)) * 5, 10)

    # Number bonus
    number_score = 8 if _NUMBER.search(candidate) else 0

    # Visual layout — now a tiebreaker bonus (0–12), not a hard reject
    layout_bonus = _layout_bonus(candidate)

    return (
        overlap * 40
        + length_score
        + action_score
        + closing_verb_bonus
        + impact_score
        + number_score
        + layout_bonus
    )


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _safe_fallback(title: str) -> str:
    """Produce a clean fallback headline when all scoring fails.

    Tries to cut at the last natural clause boundary so the result always
    reads as a complete thought. Takes at most 10 words.
    """
    cleaned = _clean(_title_candidates(title)[0])
    words = _WORDS.findall(cleaned)

    # Hard cap at 10 words for the fallback (less likely to truncate mid-thought)
    words = words[:10]

    # Trim trailing incomplete-sounding words
    while words and words[-1].lower().strip(".,") in _INCOMPLETE_ENDINGS:
        words.pop()

    return " ".join(words).upper() if words else "LATEST NEWS UPDATE"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normal(value: str) -> str:
    return value.lower().replace(",", "").strip()


def _layout_bonus(candidate: str) -> float:
    """Return a small visual layout bonus (0–12). Never rejects a headline.

    2–4 lines accepted (old code rejected anything outside 2–3).
    Balance threshold lowered from 0.42 to 0.30.
    Returns 0 if the font is unavailable rather than crashing.
    """
    try:
        font = ImageFont.truetype(str(_HEADLINE_FONT), _NORMAL_FONT_SIZE)
    except OSError:
        return 0.0

    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: list[str] = []
    current = ""
    for word in candidate.split():
        proposed = f"{current} {word}".strip()
        if draw.textlength(proposed, font=font) <= _CARD_TEXT_WIDTH:
            current = proposed
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    if not lines or len(lines) > 4:
        return 0.0
    if len(lines[-1].split()) == 1 and len(lines) > 1:
        # Single orphan word on last line — small penalty but not a rejection
        return 2.0

    widths = [float(draw.textlength(line, font=font)) for line in lines]
    balance = min(widths) / max(widths) if max(widths) > 0 else 0.0
    if balance < 0.30:
        return 2.0   # Poor balance but still valid — minimal bonus

    # Good balance: 2-line layout gets slight edge over 3–4 line
    line_bonus = {2: 6, 3: 4, 4: 2}.get(len(lines), 0)
    return balance * 6 + line_bonus
