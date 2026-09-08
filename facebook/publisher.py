"""
facebook/publisher.py — Publish a draft post to a Facebook Page via the Graph API.

Supports verified image-card posts through the Facebook photos endpoint.

Requires:
  FACEBOOK_PAGE_ID    — Your Facebook Page ID
  FACEBOOK_PAGE_TOKEN — A Page Access Token with pages_manage_posts permission
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

import requests

import config
from facebook.token_manager import TokenExpiredError, TokenManager
from models import DraftStatus, Story, VerificationStatus

logger = logging.getLogger(__name__)

GRAPH_API_URL = "https://graph.facebook.com/v19.0"


class FacebookPublishError(Exception):
    """Raised when publishing to Facebook fails."""

class FatalPublishError(Exception):
    """Raised on unrecoverable errors (expired token, invalid token).
    Agent must stop all further publishing immediately."""


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
    """Preserve the generated two-line hook and append missing hashtags."""
    message = post_content.strip()

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
    return _publish(story, image_path=None)


def publish_post_with_image(story: Story, image_path: Path) -> str:
    """
    Publish the story's post content WITH an image to the configured Facebook Page.
    Uses the /photos endpoint to attach the image.
    Returns the Facebook post ID on success.
    Raises FacebookPublishError on failure.
    """
    return _publish(story, image_path=image_path)


# ---------------------------------------------------------------------------
# Internal publish logic
# ---------------------------------------------------------------------------

def _publish(story: Story, image_path: Optional[Path]) -> str:
    """Core publish logic with mandatory independent verification and image card."""
    if story.draft_status != DraftStatus.READY_FOR_REVIEW:
        raise FacebookPublishError(
            f"Cannot publish a draft with status '{story.draft_status.value}'. "
            "Only READY_FOR_REVIEW drafts can be published."
        )

    if story.verification_status != VerificationStatus.VERIFIED:
        raise FacebookPublishError(
            "Cannot publish a story without independent-source verification."
        )

    if not story.post_content:
        raise FacebookPublishError("Story has no post content to publish.")

    if image_path is None or not image_path.exists():
        raise FacebookPublishError("A valid image card is required before publishing.")

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
        raise FatalPublishError(
            f"🔴 ACCESS TOKEN EXPIRED — {exc}\n"
            "Please get a fresh token from Facebook and update FACEBOOK_PAGE_TOKEN in .env"
        ) from exc

    return _publish_with_photo(message, image_path, token)


def _publish_text_only(message: str, token: str) -> str:
    """POST to /feed for a text-only post."""
    url = f"{GRAPH_API_URL}/{config.FACEBOOK_PAGE_ID}/feed"
    payload = {"message": message, "access_token": token}

    logger.info("Publishing to Facebook Page %s ...", config.FACEBOOK_PAGE_ID)
    return _post_with_retry(url, payload=payload)


def _publish_with_photo(message: str, image_path: Path, token: str) -> str:
    """POST to /photos to publish image + caption."""
    url = f"{GRAPH_API_URL}/{config.FACEBOOK_PAGE_ID}/photos"

    logger.info("Publishing image post to Facebook Page %s ...", config.FACEBOOK_PAGE_ID)

    last_error = None
    response = None
    for attempt in range(1, 3):
        try:
            with open(image_path, "rb") as f:
                response = requests.post(
                    url,
                    data={"caption": message, "access_token": token},
                    files={"source": (image_path.name, f, "image/jpeg")},
                    timeout=30,
                )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            detail = _extract_fb_error(response)
            _check_fatal_error(response)  # stops immediately on token errors
            if attempt < 2:
                logger.warning("Image publish attempt %d failed%s. Retrying...", attempt, detail)
            else:
                raise FacebookPublishError(
                    f"Image publish failed after 2 attempts{detail}"
                ) from last_error

    _check_fatal_error(response)  # final check on success response too
    data = response.json()
    if "error" in data:
        raise FacebookPublishError(_extract_fb_error(response))

    post_id = data.get("post_id") or data.get("id", "unknown")
    logger.info("✓ Published successfully with image. Facebook post ID: %s", post_id)
    return post_id


def _post_with_retry(url: str, payload: dict) -> str:
    """POST with up to 2 attempts, returns post ID."""
    last_error = None
    response = None
    for attempt in range(1, 3):
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            detail = _extract_fb_error(response)
            _check_fatal_error(response)  # stops immediately on token errors
            if attempt < 2:
                logger.warning("Publish attempt %d failed%s. Retrying once...", attempt, detail)
            else:
                raise FacebookPublishError(
                    f"Publish failed after 2 attempts{detail}"
                ) from last_error

    _check_fatal_error(response)
    data = response.json()
    if "error" in data:
        raise FacebookPublishError(_extract_fb_error(response))

    post_id = data.get("id", "unknown")
    logger.info("✓ Published successfully. Facebook post ID: %s", post_id)
    return post_id


def _check_fatal_error(response: Optional[requests.Response]) -> None:
    """Raise FatalPublishError immediately if the response indicates a token/auth error."""
    if response is None:
        return
    try:
        err = response.json().get("error", {})
        code = err.get("code")
        msg  = err.get("message", "")
        # Facebook auth error codes: 190 = invalid/expired token, 102 = session expired
        if code in (190, 102, 2500):
            raise FatalPublishError(
                f"\n🔴 TOKEN ERROR — Facebook API Error {code}: {msg}\n"
                "Your access token has expired or is invalid.\n"
                "Please get a fresh token and update FACEBOOK_PAGE_TOKEN in .env"
            )
    except FatalPublishError:
        raise
    except Exception:
        pass


def _extract_fb_error(response: Optional[requests.Response]) -> str:
    """Extract a readable error string from a Facebook API response."""
    if response is None:
        return ""
    try:
        err_data = response.json().get("error", {})
        code = err_data.get("code", "?")
        subcode = err_data.get("error_subcode", "")
        msg = err_data.get("message", "")
        err_type = err_data.get("type", "")
        return f" | FB Error {code}{f'/{subcode}' if subcode else ''} [{err_type}]: {msg}"
    except Exception:
        return f" | Raw response: {response.text[:200]}"
