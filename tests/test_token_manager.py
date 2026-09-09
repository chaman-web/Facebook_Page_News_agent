from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from facebook.token_manager import TokenManager


def test_sixty_day_token_is_not_reexchanged(monkeypatch):
    monkeypatch.setattr(config, "FACEBOOK_PAGE_TOKEN", "existing-token")
    monkeypatch.setattr(config, "FACEBOOK_APP_ID", "app-id")
    monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "app-secret")
    expires_at = int((datetime.now(timezone.utc) + timedelta(days=55)).timestamp())
    manager = TokenManager()

    with (
        patch.object(manager, "_debug_token", return_value={"is_valid": True, "expires_at": expires_at}),
        patch.object(manager, "_exchange_for_long_lived") as exchange,
    ):
        assert manager.get_valid_token() == "existing-token"

    exchange.assert_not_called()


def test_short_lived_token_is_exchanged_when_app_credentials_exist(monkeypatch):
    monkeypatch.setattr(config, "FACEBOOK_PAGE_TOKEN", "short-token")
    monkeypatch.setattr(config, "FACEBOOK_APP_ID", "app-id")
    monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "app-secret")
    expires_at = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    manager = TokenManager()

    with (
        patch.object(manager, "_debug_token", return_value={"is_valid": True, "expires_at": expires_at}),
        patch.object(manager, "_exchange_for_long_lived", return_value="long-token") as exchange,
        patch.object(manager, "_save_token_to_env") as save,
    ):
        assert manager.get_valid_token() == "long-token"

    exchange.assert_called_once_with("short-token")
    save.assert_called_once_with("long-token")
