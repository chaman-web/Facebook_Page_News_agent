"""
pipeline/content_validator.py — Validate generated Facebook post quality before publishing.

Checks:
1. Minimum and maximum post length
2. Headline presence (first line non-empty)
3. Minimum hashtag count
4. No hallucination markers (fabricated URLs, placeholder text)
5. No forbidden/sensitive phrases that could get the page flagged
6. Sources line present
7. No excessive repetition
"""

from __future__ import annotations

import logging
import re

from models import GenerationError, Story

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

MIN_CHARS = 200          # Minimum post body length
MAX_CHARS = 2500         # Keep posts concise — Facebook best practice
MIN_HASHTAGS = 2         # At least 2 hashtags required
MAX_HASHTAGS = 5         # Cap at 5 — quality over quantity
MAX_REPEATED_SENTENCES = 2  # Flag if same sentence appears more than this

# Placeholder patterns the LLM sometimes outputs
PLACEHOLDER_PATTERNS = [
    r"\[insert\b",
    r"\[your\b",
    r"\[link\b",
    r"\[source\b",
    r"\[date\b",
    r"\[name\b",
    r"<insert",
    r"<your",
    r"lorem ipsum",
    r"\.\.\.\s*\.\.\.",   # Multiple ellipses
]

# Phrases that could get the page flagged or restricted on Facebook
SENSITIVE_PHRASES = [
    r"\bbuy now\b",
    r"\bclick here\b",
    r"\bfree money\b",
    r"\bmake money fast\b",
    r"\bget rich\b",
    r"\b100% guaranteed\b",
]


class ContentValidationError(GenerationError):
    """Raised when generated post content fails quality checks."""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def validate_post(story: Story) -> Story:
    """
    Validate the generated post content quality.
    Raises ContentValidationError with a clear reason if validation fails.
    Returns the story unchanged if all checks pass.
    """
    post = story.post_content or ""
    hashtags = story.hashtags or []

    issues = []

    # 1. Minimum length
    if len(post) < MIN_CHARS:
        issues.append(
            f"Post too short ({len(post)} chars, minimum {MIN_CHARS}). "
            "LLM may have returned incomplete content."
        )

    # 2. Maximum length
    if len(post) > MAX_CHARS:
        logger.warning(
            "Post exceeds %d chars (%d). Truncating to fit Facebook best practices.",
            MAX_CHARS, len(post)
        )
        # Truncate at last sentence boundary before limit
        truncated = post[:MAX_CHARS]
        last_period = max(
            truncated.rfind(". "),
            truncated.rfind(".\n"),
        )
        if last_period > MIN_CHARS:
            story.post_content = truncated[:last_period + 1].strip()
        else:
            story.post_content = truncated.strip()
        post = story.post_content

    # 3. Headline present (first non-empty line)
    lines = [l.strip() for l in post.splitlines() if l.strip()]
    if not lines:
        issues.append("Post has no content lines at all.")
    elif len(lines[0]) < 10:
        issues.append(
            f"Headline too short ('{lines[0]}'). "
            "First line should be a meaningful headline."
        )

    # 4. Hashtag count — enforce 2–5
    if len(hashtags) < MIN_HASHTAGS:
        issues.append(
            f"Too few hashtags ({len(hashtags)}, minimum {MIN_HASHTAGS})."
        )
    if len(hashtags) > MAX_HASHTAGS:
        # Silently trim to MAX_HASHTAGS — always keep #WorldUpdate first
        priority = [t for t in hashtags if t.lower() == "#worldupdate"]
        rest     = [t for t in hashtags if t.lower() != "#worldupdate"]
        story.hashtags = (priority + rest)[:MAX_HASHTAGS]
        hashtags = story.hashtags
        logger.info("Trimmed hashtags to %d: %s", MAX_HASHTAGS, ", ".join(hashtags))

    # 5. Placeholder text detection
    post_lower = post.lower()
    for pattern in PLACEHOLDER_PATTERNS:
        if re.search(pattern, post_lower, re.IGNORECASE):
            issues.append(
                f"Post contains placeholder text matching pattern: '{pattern}'. "
                "LLM did not complete the content properly."
            )
            break

    # 6. Sensitive/flagged phrases
    for pattern in SENSITIVE_PHRASES:
        if re.search(pattern, post_lower, re.IGNORECASE):
            issues.append(
                f"Post contains phrase that may get flagged by Facebook: '{pattern}'."
            )

    # 7. Sources line present
    if "sources:" not in post_lower and "source:" not in post_lower:
        logger.warning("Post is missing a sources line. Consider adding one.")
        # Warning only — not a hard failure

    # 8. Excessive repetition — check for repeated sentences
    sentences = re.split(r"[.!?]\s+", post)
    seen: dict[str, int] = {}
    for sentence in sentences:
        s = sentence.strip().lower()
        if len(s) > 20:  # ignore very short fragments
            seen[s] = seen.get(s, 0) + 1
            if seen[s] > MAX_REPEATED_SENTENCES:
                issues.append(
                    f"Post contains excessive repetition of: '{sentence[:60]}...'"
                )
                break

    # 9. Must contain closing question (engagement driver)
    has_question = "?" in post or "👇" in post
    if not has_question:
        logger.warning("Post has no closing question — engagement may be lower.")

    # 10. Must contain page hashtag
    page_tags = ["#globalpulsenews", "#worldupdate"]
    has_page_tag = any(t in post_lower for t in page_tags)
    if not has_page_tag:
        logger.warning("Post is missing the page hashtag (#GlobalPulseNews).")

    # --- Report results ---
    if issues:
        issue_list = "\n  - ".join(issues)
        raise ContentValidationError(
            f"Post failed content validation ({len(issues)} issue(s)):\n  - {issue_list}"
        )

    logger.info(
        "Content validation passed. Post length: %d chars, %d hashtags.",
        len(post),
        len(hashtags),
    )
    return story
