"""
facebook/token_validator.py — Fast pre-flight token check before any publishing run.

Performs a lightweight GET /me?fields=id call instead of the heavier /debug_token.
Fails in under 3 seconds with a clear human-readable message.

Usage (in agent.py):
    from facebook.token_validator import validate_token_or_exit
    validate_token_or_exit()   # call once before the publishing loop starts
"""

from __future__ import annotations

import logging
import sys

import requests

import config
from facebook.token_manager import TokenExpiredError, TokenManager

logger = logging.getLogger(__name__)

GRAPH_API_URL = f"https://graph.facebook.com/{config.FACEBOOK_GRAPH_API_VERSION}"
_VALIDATION_TIMEOUT = 6   # seconds — fast fail, don't block the run


def validate_token_or_exit(exit_on_failure: bool = True) -> bool:
    """
    Validate the Facebook token with a lightweight /me?fields=id call.

    Returns True if valid.
    If invalid and exit_on_failure=True (default): logs a clear error and exits.
    If invalid and exit_on_failure=False: returns False (useful for dry-run checks).
    """
    token = config.FACEBOOK_PAGE_TOKEN
    if not token:
        msg = (
            "\n🔴 FACEBOOK_PAGE_TOKEN is not set in .env.\n"
            "   Get a token from: https://developers.facebook.com/tools/explorer\n"
            "   Then set FACEBOOK_PAGE_TOKEN=<your_token> in .env\n"
        )
        logger.error(msg)
        if exit_on_failure:
            sys.exit(1)
        return False

    logger.info("🔑 Validating Facebook token...")
    try:
        resp = requests.get(
            f"{GRAPH_API_URL}/me",
            params={"fields": "id,name", "access_token": token},
            timeout=_VALIDATION_TIMEOUT,
        )
        data = resp.json()

        # Token error
        if "error" in data:
            err   = data["error"]
            code  = err.get("code")
            msg   = err.get("message", "Unknown error")
            etype = err.get("type", "")
            _print_token_error(code, etype, msg)
            if exit_on_failure:
                sys.exit(1)
            return False

        # Success — token is valid
        page_id   = data.get("id", "unknown")
        page_name = data.get("name", "")
        logger.info(
            "✅ Token valid — Page: %s (ID: %s)",
            page_name or "unknown", page_id,
        )

        # Also run the full TokenManager check for expiry warning
        try:
            TokenManager().get_valid_token()
        except TokenExpiredError as exc:
            logger.warning("⚠️  Token expiry warning: %s", str(exc).splitlines()[0])

        return True

    except requests.ConnectionError:
        logger.error(
            "🔴 Cannot reach Facebook API — check your internet connection.\n"
            "   Publishing skipped for this run."
        )
        if exit_on_failure:
            sys.exit(1)
        return False

    except requests.Timeout:
        logger.error(
            "🔴 Facebook API did not respond within %ds.\n"
            "   Publishing skipped for this run.", _VALIDATION_TIMEOUT
        )
        if exit_on_failure:
            sys.exit(1)
        return False

    except Exception as exc:
        logger.error("🔴 Token validation failed unexpectedly: %s", exc)
        if exit_on_failure:
            sys.exit(1)
        return False


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
