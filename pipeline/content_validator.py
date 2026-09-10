"""
pipeline/content_validator.py — Validate generated Facebook post quality before publishing.

Hard checks (block publish — genuinely unfixable):
  1. Post completely empty
  2. Placeholder text (LLM left template unfilled)
  3. Facebook policy-violating phrases

Auto-fixed (never rejected):
  - Post too long → truncated at sentence boundary
  - Too many hashtags → trimmed, keeping page tag first
  - Post/headline mismatch → missing key terms prepended as context sentence

Soft checks (warn only, always publish):
  - Post shorter than ideal
  - Sources line missing
  - Hashtag count low
  - Repeated sentences

Philosophy: fix it, don't reject it.
"""

from __future__ import annotations

import logging
import re

from models import GenerationError, Story
from pipeline.caption_sanitizer import sanitize_facebook_caption

logger = logging.getLogger(__name__)

MIN_CHARS   = 60
MAX_CHARS   = 2500
MIN_HASHTAGS = 2
MAX_HASHTAGS = 5
MAX_REPEATED_SENTENCES = 2

PLACEHOLDER_PATTERNS = [
    r"\[insert\b", r"\[your\b", r"\[link\b", r"\[source\b",
    r"\[date\b", r"\[name\b", r"<insert", r"<your",
    r"lorem ipsum", r"\.\.\.\s*\.\.\.",
]

POLICY_PHRASES = [
    r"\bbuy now\b", r"\bclick here\b", r"\bfree money\b",
    r"\bmake money fast\b", r"\bget rich\b", r"\b100% guaranteed\b",
]

# Words that carry no meaningful signal for matching
_STOP_WORDS = {
    "a", "an", "the", "is", "in", "on", "at", "to", "of", "and", "or",
    "for", "with", "after", "as", "by", "be", "are", "was", "were",
    "has", "have", "had", "that", "this", "it", "its", "says", "say",
    "will", "from", "over", "into", "out", "up", "than", "but", "not",
    "new", "more", "about", "been", "also", "their", "they", "which",
}


def _key_terms(text: str) -> set[str]:
    """Extract meaningful words (≥4 chars, not stop words) from text."""
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    return {w for w in words if w not in _STOP_WORDS}


class ContentValidationError(GenerationError):
    """Raised only when the post has an unfixable hard problem."""


def validate_post(story: Story) -> Story:
    """
    Validate and auto-fix generated post content.
    Only raises ContentValidationError for truly unfixable issues.
    Everything else: warn and continue.
    """
    story.post_content = sanitize_facebook_caption(story.post_content)
    post     = story.post_content
    hashtags = story.hashtags or []

    # ── HARD: empty post ────────────────────────────────────────────────────
    if len(post.strip()) < MIN_CHARS:
        raise ContentValidationError(
            f"Post is empty or too short ({len(post)} chars) — LLM returned no usable content."
        )

    # ── AUTO-FIX: too long → truncate at sentence boundary ──────────────────
    if len(post) > MAX_CHARS:
        logger.warning("Post too long (%d chars) — truncating to %d.", len(post), MAX_CHARS)
        truncated   = post[:MAX_CHARS]
        last_period = max(truncated.rfind(". "), truncated.rfind(".\n"))
        story.post_content = (
            truncated[:last_period + 1] if last_period > MIN_CHARS else truncated
        ).strip()
        post = story.post_content

    post_lower = post.lower()

    # ── HARD: placeholder text (LLM didn't complete) ─────────────────────────
    for pattern in PLACEHOLDER_PATTERNS:
        if re.search(pattern, post_lower, re.IGNORECASE):
            raise ContentValidationError(
                f"Post contains unfilled placeholder '{pattern}' — LLM did not complete content."
            )

    # ── HARD: Facebook policy violations ────────────────────────────────────
    for pattern in POLICY_PHRASES:
        if re.search(pattern, post_lower, re.IGNORECASE):
            raise ContentValidationError(
                f"Post contains Facebook policy-violating phrase: '{pattern}'."
            )

    # ── AUTO-FIX: too many hashtags → trim, keep page tag ───────────────────
    if len(hashtags) > MAX_HASHTAGS:
        priority = [t for t in hashtags if t.lower() in ("#globalpulsenews", "#worldupdate")]
        rest     = [t for t in hashtags if t.lower() not in ("#globalpulsenews", "#worldupdate")]
        story.hashtags = (priority + rest)[:MAX_HASHTAGS]
        hashtags = story.hashtags
        logger.info("Hashtags trimmed to %d: %s", MAX_HASHTAGS, ", ".join(hashtags))

    # ── AUTO-FIX: post/headline mismatch ────────────────────────────────────
    # The image card headline and the post body must be about the same story.
    # If fewer than 2 key terms from the headline appear in the post, the reader
    # sees a card about topic A but post text about topic B — confusing.
    # Fix: prepend a clear context sentence using the headline and source.
    headline_text  = story.card_headline or story.title
    headline_terms = _key_terms(headline_text)
    post_terms     = _key_terms(post)
    overlap        = headline_terms & post_terms
    required       = max(2, int(len(headline_terms) * 0.35))   # at least 35% of headline terms

    if len(overlap) < required and len(headline_terms) >= 3:
        context_sentence = (
            f"📌 {headline_text} — via {story.source_name}.\n\n"
        )
        story.post_content = context_sentence + post
        post = story.post_content
        logger.warning(
            "Post/headline mismatch (only %d/%d key terms matched) — "
            "prepended headline context. Title: '%s'",
            len(overlap), required, story.title[:60],
        )

    # ── AUTO-FIX: ensure enough readable content ────────────────────────────
    # Strip hashtags, emoji, source lines — count real sentence content only.
    body_only = re.sub(r"#\w+", "", post)
    body_only = re.sub(r"[^\w\s.!?,']", " ", body_only)
    body_only = re.sub(r"\s+", " ", body_only).strip()
    sentences = [s.strip() for s in re.split(r"[.!?]", body_only) if len(s.strip()) > 20]

    if len(sentences) < 2:
        # Only one real sentence — enrich with summary if available
        summary = (story.raw_summary or "").strip()
        if summary and len(summary) > 40:
            first_sentence = summary.split(".")[0].strip()
            if first_sentence.lower() not in post.lower():
                story.post_content = story.post_content.rstrip() + f"\n\n{first_sentence}."
                post = story.post_content
                logger.warning(
                    "Post had only 1 real sentence — appended summary context for '%s'",
                    story.title[:60],
                )

    # ── SOFT warnings — never block ──────────────────────────────────────────
    summary_len = len(story.raw_summary or "")
    ideal_min   = 150 if summary_len > 200 else (80 if summary_len > 50 else MIN_CHARS)
    if len(post) < ideal_min:
        logger.warning(
            "Post shorter than ideal (%d chars, ideal %d) — publishing anyway.", len(post), ideal_min
        )

    if len(hashtags) < MIN_HASHTAGS:
        logger.warning("Only %d hashtag(s), preferred minimum is %d.", len(hashtags), MIN_HASHTAGS)

    if "sources:" not in post_lower and "source:" not in post_lower:
        logger.warning("Post has no sources line — minor quality note.")

    seen_sents: dict[str, int] = {}
    for sentence in re.split(r"[.!?]\s+", post):
        s = sentence.strip().lower()
        if len(s) > 20:
            seen_sents[s] = seen_sents.get(s, 0) + 1
            if seen_sents[s] > MAX_REPEATED_SENTENCES:
                logger.warning("Repeated sentence detected: '%s...'", s[:60])
                break

    logger.info("✅ Content validation passed (%d chars, %d hashtags).", len(post), len(hashtags))
    return story
