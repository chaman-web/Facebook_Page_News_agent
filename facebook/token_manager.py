"""
facebook/token_manager.py — Manages Facebook Page Access Token lifecycle.

Flow:
  1. Load token from .env
  2. Validate token via Graph API /debug_token
  3. If short-lived → exchange for long-lived token (60 days) and save to .env
  4. If expired → raise clear error with instructions to re-authenticate
  5. Return valid token for publishing

Requires in .env:
  FACEBOOK_PAGE_TOKEN   — Page Access Token
  FACEBOOK_PAGE_ID      — Page ID
  FACEBOOK_APP_ID       — Your Meta App ID
  FACEBOOK_APP_SECRET   — Your Meta App Secret
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

import config

logger = logging.getLogger(__name__)

GRAPH_API_URL = "https://graph.facebook.com/v19.0"
ENV_PATH = config.ENV_PATH

# Tokens with more than this many seconds remaining are considered valid
MIN_REMAINING_SECONDS = 60 * 60  # 1 hour


class TokenExpiredError(Exception):
    """Raised when the token is expired and cannot be refreshed automatically."""


class TokenManager:
    """Validates and refreshes Facebook Page Access Tokens."""

    def get_valid_token(self) -> str:
        """
        Returns a valid Page Access Token.
        - If current token is valid and long-lived: return as-is
        - If current token is short-lived: exchange for long-lived and save
        - If expired: raise TokenExpiredError with clear instructions
        """
        token = config.FACEBOOK_PAGE_TOKEN

        if not token:
            raise TokenExpiredError(
                "FACEBOOK_PAGE_TOKEN is not set in .env.\n"
                "Get a Page Access Token from: https://developers.facebook.com/tools/explorer"
            )

        info = self._debug_token(token)

        if not info.get("is_valid"):
            error_msg = info.get("error", {}).get("message", "Unknown error")
            raise TokenExpiredError(
                f"Facebook token is invalid or expired: {error_msg}\n\n"
                "To fix:\n"
                "1. Go to https://developers.facebook.com/tools/explorer\n"
                "2. Select your app → Generate Access Token\n"
                "3. Run: me/accounts → copy the page access_token\n"
                "4. Update FACEBOOK_PAGE_TOKEN in your .env file"
            )

        expires_at = info.get("expires_at", 0)
        token_type = "long-lived" if expires_at == 0 or expires_at > 5_000_000_000 else "short-lived"

        if expires_at and expires_at != 0:
            remaining = expires_at - datetime.now(timezone.utc).timestamp()
            expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            logger.info("Token is %s. Expires: %s (%.0f hours remaining)", token_type, expires_dt, remaining / 3600)

            if remaining < MIN_REMAINING_SECONDS:
                raise TokenExpiredError(
                    f"Facebook token expires soon ({expires_dt}) and cannot be auto-refreshed.\n\n"
                    "To fix:\n"
                    "1. Go to https://developers.facebook.com/tools/explorer\n"
                    "2. Select your app → Generate Access Token\n"
                    "3. Run: me/accounts → copy the page access_token\n"
                    "4. Update FACEBOOK_PAGE_TOKEN in your .env file"
                )
        else:
            logger.info("Token is long-lived (no expiry). All good.")

        # If short-lived and app credentials are available, exchange for long-lived
        if token_type == "short-lived" and config.FACEBOOK_APP_ID and config.FACEBOOK_APP_SECRET:
            logger.info("Short-lived token detected. Exchanging for long-lived token...")
            try:
                long_lived = self._exchange_for_long_lived(token)
                self._save_token_to_env(long_lived)
                logger.info("Long-lived token saved to .env (valid for ~60 days).")
                return long_lived
            except Exception as exc:
                logger.warning("Could not exchange for long-lived token: %s. Using current token.", exc)

        return token

    def _debug_token(self, token: str) -> dict:
        """Call Graph API /debug_token to inspect token validity."""
        try:
            # Use the token to inspect itself if no app credentials
            if config.FACEBOOK_APP_ID and config.FACEBOOK_APP_SECRET:
                access_token = f"{config.FACEBOOK_APP_ID}|{config.FACEBOOK_APP_SECRET}"
            else:
                access_token = token

            response = requests.get(
                f"{GRAPH_API_URL}/debug_token",
                params={"input_token": token, "access_token": access_token},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("data", {})
        except requests.RequestException as exc:
            logger.warning("Token debug request failed: %s", exc)
            # If we can't verify, assume valid and let the publish attempt reveal errors
            return {"is_valid": True, "expires_at": 0}

    def _exchange_for_long_lived(self, short_token: str) -> str:
        """Exchange a short-lived token for a long-lived one (~60 days)."""
        response = requests.get(
            f"https://graph.facebook.com/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": config.FACEBOOK_APP_ID,
                "client_secret": config.FACEBOOK_APP_SECRET,
                "fb_exchange_token": short_token,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if "access_token" not in data:
            raise ValueError(f"No access_token in response: {data}")
        return data["access_token"]

    def _save_token_to_env(self, new_token: str) -> None:
        """Update FACEBOOK_PAGE_TOKEN in the .env file."""
        if not ENV_PATH.exists():
            return
        content = ENV_PATH.read_text(encoding="utf-8")
        content = re.sub(
            r"^FACEBOOK_PAGE_TOKEN=.*$",
            f"FACEBOOK_PAGE_TOKEN={new_token}",
            content,
            flags=re.MULTILINE,
        )
        ENV_PATH.write_text(content, encoding="utf-8")
        # Reload config so the rest of the run uses the new token
        import importlib
        importlib.reload(config)
