"""
facebook/publisher.py — Publish a draft post to a Facebook Page via the Graph API.

Requires:
  FACEBOOK_PAGE_ID    — Your Facebook Page ID
  FACEBOOK_PAGE_TOKEN — A Page Access Token with pages_manage_posts permission

This module is only called when agent.py is run with --publish flag.
Phase 1 never calls this automatically.
"""

from __future__ import annotations

import logging

import requests

import config
from models import DraftStatus, Story

logger = logging.getLogger(__name__)

GRAPH_API_URL = f"https://graph.facebook.com/{config.FACEBOOK_GRAPH_API_VERSION}"


class FacebookPublishError(Exception):
    """Raised when publishing to Facebook fails."""


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

    # Build the full post message: content + hashtags on a new line
    message = story.post_content
    if story.hashtags:
        hashtag_line = " ".join(story.hashtags)
        if hashtag_line not in message:
            message = f"{message}\n\n{hashtag_line}"

    url = f"{GRAPH_API_URL}/{config.FACEBOOK_PAGE_ID}/feed"
    payload = {
        "message": message,
        "access_token": config.FACEBOOK_PAGE_TOKEN,
    }

    logger.info("Publishing to Facebook Page %s ...", config.FACEBOOK_PAGE_ID)

    try:
        response = requests.post(url, data=payload, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FacebookPublishError(f"Facebook API request failed: {exc}") from exc

    data = response.json()

    if "error" in data:
        err = data["error"]
        raise FacebookPublishError(
            f"Facebook API error {err.get('code')}: {err.get('message')}"
        )

    post_id = data.get("id", "unknown")
    logger.info("✓ Published successfully. Facebook post ID: %s", post_id)
    return post_id
