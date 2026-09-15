"""
facebook/token_validator.py — Fast pre-flight token check before any publishing run.

Checks token validity and permissions, configured Page identity, and API-visible
Page publishing restrictions before any scheduled publish starts.

Usage (in agent.py):
    from facebook.token_validator import validate_token_or_exit
    validate_token_or_exit()   # call once before the publishing loop starts
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timezone

import requests

import config
logger = logging.getLogger(__name__)

GRAPH_API_URL = f"https://graph.facebook.com/{config.FACEBOOK_GRAPH_API_VERSION}"
_VALIDATION_TIMEOUT = 15  # seconds — increased from 6 to handle slow DNS after wake
_REQUIRED_SCOPES = {"pages_manage_posts"}

# Retry config for transient network errors (DNS not ready after wake from sleep)
_NETWORK_RETRY_COUNT = 3
_NETWORK_RETRY_DELAY = 10  # seconds between retries


def _get_with_retry(url: str, params: dict, timeout: int) -> requests.Response:
    """GET with retries for transient network errors (DNS failure, connection reset)."""
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, _NETWORK_RETRY_COUNT + 1):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except requests.ConnectionError as exc:
            last_exc = exc
            if attempt < _NETWORK_RETRY_COUNT:
                logger.warning(
                    "Network error on attempt %d/%d — retrying in %ds: %s",
                    attempt, _NETWORK_RETRY_COUNT, _NETWORK_RETRY_DELAY, exc,
                )
                time.sleep(_NETWORK_RETRY_DELAY)
        except requests.Timeout as exc:
            last_exc = exc
            if attempt < _NETWORK_RETRY_COUNT:
                logger.warning(
                    "Timeout on attempt %d/%d — retrying in %ds",
                    attempt, _NETWORK_RETRY_COUNT, _NETWORK_RETRY_DELAY,
                )
                time.sleep(_NETWORK_RETRY_DELAY)
    raise last_exc


def _fail(message: str, exit_on_failure: bool) -> bool:
    logger.error("🔴 Facebook Page preflight failed — %s", message)
    if exit_on_failure:
        sys.exit(1)
    return False


def validate_token_or_exit(exit_on_failure: bool = True) -> bool:
    """
    Validate token permissions and the configured Page's publishing state.

    Returns True if valid.
    If invalid and exit_on_failure=True (default): logs a clear error and exits.
    If invalid and exit_on_failure=False: returns False (useful for dry-run checks).
    """
    token = config.FACEBOOK_PAGE_TOKEN
    if not token:
        return _fail("FACEBOOK_PAGE_TOKEN is not set in .env.", exit_on_failure)
    if not config.FACEBOOK_PAGE_ID:
        return _fail("FACEBOOK_PAGE_ID is not set in .env.", exit_on_failure)
    if not re.fullmatch(r"v\d+\.\d+", config.FACEBOOK_GRAPH_API_VERSION):
        return _fail(
            f"Invalid Graph API version '{config.FACEBOOK_GRAPH_API_VERSION}'.",
            exit_on_failure,
        )

    logger.info("🔑 Running Facebook Page publishing preflight...")
    try:
        app_access_token = (
            f"{config.FACEBOOK_APP_ID}|{config.FACEBOOK_APP_SECRET}"
            if config.FACEBOOK_APP_ID and config.FACEBOOK_APP_SECRET
            else token
        )
        debug_response = _get_with_retry(
            f"{GRAPH_API_URL}/debug_token",
            params={"input_token": token, "access_token": app_access_token},
            timeout=_VALIDATION_TIMEOUT,
        )
        debug_response.raise_for_status()
        debug_payload = debug_response.json()
        if "error" in debug_payload:
            error = debug_payload["error"]
            _print_token_error(error.get("code"), error.get("type", ""), error.get("message", ""))
            return _fail("Token debug request was rejected.", exit_on_failure)
        token_info = debug_payload.get("data", {})
        if not token_info.get("is_valid"):
            return _fail("Page access token is invalid or expired.", exit_on_failure)

        scopes = set(token_info.get("scopes") or [])
        scopes.update(
            item.get("scope", "") for item in token_info.get("granular_scopes") or []
        )
        missing_scopes = sorted(_REQUIRED_SCOPES - scopes)
        if missing_scopes:
            return _fail(
                "Missing required permission(s): " + ", ".join(missing_scopes),
                exit_on_failure,
            )

        granular_posts = next(
            (
                item for item in token_info.get("granular_scopes") or []
                if item.get("scope") == "pages_manage_posts"
            ),
            None,
        )
        if granular_posts and granular_posts.get("target_ids"):
            if str(config.FACEBOOK_PAGE_ID) not in {str(value) for value in granular_posts["target_ids"]}:
                return _fail(
                    "pages_manage_posts is not granted for the configured Page ID.",
                    exit_on_failure,
                )

        now_ts = datetime.now(timezone.utc).timestamp()
        for label, value in (
            ("token", token_info.get("expires_at", 0)),
            ("data access", token_info.get("data_access_expires_at", 0)),
        ):
            if value and value <= now_ts:
                return _fail(f"Facebook {label} has expired.", exit_on_failure)

        page_response = _get_with_retry(
            f"{GRAPH_API_URL}/{config.FACEBOOK_PAGE_ID}",
            params={
                "fields": "id,name,is_published,can_post",
                "access_token": token,
            },
            timeout=_VALIDATION_TIMEOUT,
        )
        page_response.raise_for_status()
        page = page_response.json()
        if "error" in page:
            error = page["error"]
            return _fail(
                f"Page status request failed: {error.get('message', 'Unknown error')}",
                exit_on_failure,
            )
        if str(page.get("id")) != str(config.FACEBOOK_PAGE_ID):
            return _fail("Token Page ID does not match FACEBOOK_PAGE_ID.", exit_on_failure)
        if page.get("is_published") is False:
            return _fail("The configured Facebook Page is not published.", exit_on_failure)
        if page.get("can_post") is False:
            return _fail("Facebook currently restricts posting to this Page.", exit_on_failure)

        logger.info(
            "✅ Facebook Page preflight passed — %s (ID: %s), %s, pages_manage_posts granted.",
            page.get("name") or "unknown",
            page.get("id"),
            config.FACEBOOK_GRAPH_API_VERSION,
        )
        return True

    except requests.RequestException as exc:
        return _fail(f"Graph API request failed: {exc}", exit_on_failure)
    except (TypeError, ValueError) as exc:
        return _fail(f"Invalid Graph API response: {exc}", exit_on_failure)


def _print_token_error(code: int | None, etype: str, msg: str) -> None:
    if code in (190, 102, 2500):
        logger.error(
            "\n🔴 TOKEN EXPIRED (FB Error %s: %s)\n"
            "\n"
            "   To fix:\n"
            "   1. Go to https://developers.facebook.com/tools/explorer\n"
            "   2. Select your app → click 'Generate Access Token'\n"
            "   3. Click 'Get Token' → 'Get Page Access Token'\n"
            "   4. Copy the token and update FACEBOOK_PAGE_TOKEN in .env\n"
            "   5. Re-run the agent.\n",
            code, msg,
        )
    elif code == 4 or code == 17:
        logger.error(
            "\n🔴 RATE LIMITED (FB Error %s)\n"
            "   Facebook is throttling requests. Wait a few minutes and try again.\n",
            code,
        )
    else:
        logger.error(
            "\n🔴 TOKEN INVALID (FB Error %s [%s]: %s)\n"
            "   Update FACEBOOK_PAGE_TOKEN in .env and try again.\n",
            code, etype, msg,
        )
