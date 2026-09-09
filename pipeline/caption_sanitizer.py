"""Final cleanup for text that will appear in a Facebook caption."""

from __future__ import annotations

import html
import re


_BLANK_LINE_TOKEN = re.compile(
    r"(?i)(?:\[\s*blank\s+line\s*\]?|\bblank\s+line\s*\]|<\s*blank\s+line\s*>)"
)
_BREAK_TAG = re.compile(r"(?is)<\s*(?:br\s*/?|/?p(?:\s+[^>]*)?)\s*>")
_HTML_TAG = re.compile(r"(?is)<[^>]+>")


def sanitize_facebook_caption(text: str | None) -> str:
    """Remove template/HTML artifacts while preserving readable paragraphs."""
    if not text:
        return ""

    cleaned = html.unescape(text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _BREAK_TAG.sub("\n", cleaned)
    cleaned = _HTML_TAG.sub("", cleaned)
    cleaned = _BLANK_LINE_TOKEN.sub("", cleaned)

    # Captions are plain text. Remove unmatched markup delimiters that may have
    # survived a malformed model response, then normalize paragraph spacing.
    cleaned = cleaned.replace("<", "").replace(">", "")
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines())
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
