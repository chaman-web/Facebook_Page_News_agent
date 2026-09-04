"""
facebook/publisher.py — Publish a draft post to a Facebook Page via the Graph API.

Requires:
  FACEBOOK_PAGE_ID    — Your Facebook Page ID
  FACEBOOK_PAGE_TOKEN — A Page Access Token with pages_manage_posts permission
"""

from __future__ import annotations

import logging
import re
import unicodedata

import requests

import config
from facebook.token_manager import TokenExpiredError, TokenManager
from models import DraftStatus, Story

logger = logging.getLogger(__name__)

GRAPH_API_URL = "https://graph.facebook.com/v19.0"


class FacebookPublishError(Exception):
    """Raised when publishing to Facebook fails."""


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Sanitise post text — normalize unicode and replace fancy punctuation."""
    text = unicodedata.normalize("NFC", text)
    replacements = {
        "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^\S\n\t ]+", " ", text)
    return text.strip()


def _format_post(post_content: str, hashtags: list[str]) -> str:
    """
    Format the final Facebook post:
    - Headline on first line with red dot emoji + UPPERCASE for visual emphasis
    - Body as normal text
    - Hashtags appended at the end
    """
    lines = [l for l in post_content.strip().splitlines() if l.strip()]
    if not lines:
        return post_content

    headline = "BREAKING: " + lines[0].strip()
    body = "\n".join(lines[1:]).strip()

    message = headline
    if body:
        message = f"{headline}\n\n{body}"

    if hashtags:
        hashtag_line = " ".join(hashtags)
        if hashtag_line not in message:
            message = f"{message}\n\n{hashtag_line}"

    return message


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def publish_post(story: Story) -> str:
    """
    Publish the story's post content to the configured Facebook Page.
    Returns the Facebook post ID on success.
    Raises FacebookPublishError on failure.
    """
    if story.draft_status != DraftStatus.READY_FOR_REVIEW:
        raise FacebookPublishError(
            f"Cannot publish a draft with status '{story.draft_status.value}'. "
            "Only READY_FOR_REVIEW drafts can be published."
        )

    if not story.post_content:
        raise FacebookPublishError("Story has no post content to publish.")

    if not config.FACEBOOK_PAGE_ID or not config.FACEBOOK_PAGE_TOKEN:
        raise FacebookPublishError(
            "FACEBOOK_PAGE_ID and FACEBOOK_PAGE_TOKEN must be set in .env to publish."
        )

    clean_content = _clean_text(story.post_content)
    message = _format_post(clean_content, story.hashtags)

    # Validate / refresh token before publishing
    try:
        token = TokenManager().get_valid_token()
    except TokenExpiredError as exc:
        raise FacebookPublishError(str(exc)) from exc

    url = f"{GRAPH_API_URL}/{config.FACEBOOK_PAGE_ID}/feed"
    payload = {
        "message": message,
        "access_token": token,
    }

    logger.info("Publishing to Facebook Page %s ...", config.FACEBOOK_PAGE_ID)

    last_error = None
    response = None
    for attempt in range(1, 3):  # Max 2 attempts
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            break  # Success — exit retry loop
        except requests.RequestException as exc:
            last_error = exc
            # Try to extract Facebook's detailed error from response body
            detail = ""
            if response is not None:
                try:
                    err_data = response.json().get("error", {})
                    code = err_data.get("code", "?")
                    subcode = err_data.get("error_subcode", "")
                    msg = err_data.get("message", "")
                    err_type = err_data.get("type", "")
                    detail = f" | FB Error {code}{f'/{subcode}' if subcode else ''} [{err_type}]: {msg}"
                except Exception:
                    detail = f" | Raw response: {response.text[:200]}"

            if attempt < 2:
                logger.warning("Publish attempt %d failed%s. Retrying once...", attempt, detail)
            else:
                raise FacebookPublishError(
                    f"Publish failed after 2 attempts{detail}"
                ) from last_error

    data = response.json()

    if "error" in data:
        err = data["error"]
        code = err.get("code", "?")
        subcode = err.get("error_subcode", "")
        msg = err.get("message", "Unknown error")
        err_type = err.get("type", "")
        raise FacebookPublishError(
            f"FB Error {code}{f'/{subcode}' if subcode else ''} [{err_type}]: {msg}"
        )

    post_id = data.get("id", "unknown")
    logger.info("✓ Published successfully. Facebook post ID: %s", post_id)
    return post_id
