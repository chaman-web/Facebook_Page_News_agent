from unittest.mock import Mock, patch

import config
from facebook.token_validator import validate_token_or_exit


def _response(payload: dict) -> Mock:
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _valid_debug(scopes=None, target_ids=None) -> dict:
    scopes = scopes if scopes is not None else ["pages_manage_posts", "pages_read_engagement"]
    target_ids = target_ids if target_ids is not None else ["page-123"]
    return {
        "data": {
            "is_valid": True,
            "expires_at": 0,
            "data_access_expires_at": 0,
            "scopes": scopes,
            "granular_scopes": [
                {"scope": "pages_manage_posts", "target_ids": target_ids}
            ] if "pages_manage_posts" in scopes else [],
        }
    }


def _configure(monkeypatch):
    monkeypatch.setattr(config, "FACEBOOK_PAGE_TOKEN", "page-token")
    monkeypatch.setattr(config, "FACEBOOK_PAGE_ID", "page-123")
    monkeypatch.setattr(config, "FACEBOOK_GRAPH_API_VERSION", "v25.0")
    monkeypatch.setattr(config, "FACEBOOK_APP_ID", "")
    monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "")


def test_page_preflight_checks_permission_identity_and_posting_state(monkeypatch):
    _configure(monkeypatch)
    page = {"id": "page-123", "name": "World Update", "is_published": True, "can_post": True}
    with patch(
        "facebook.token_validator.requests.get",
        side_effect=[_response(_valid_debug()), _response(page)],
    ) as get:
        assert validate_token_or_exit(exit_on_failure=False) is True

    assert get.call_count == 2
    assert get.call_args_list[0].args[0].endswith("/debug_token")
    assert get.call_args_list[1].args[0].endswith("/page-123")


def test_page_preflight_rejects_missing_manage_posts_permission(monkeypatch):
    _configure(monkeypatch)
    with patch(
        "facebook.token_validator.requests.get",
        return_value=_response(_valid_debug(scopes=["pages_read_engagement"])),
    ):
        assert validate_token_or_exit(exit_on_failure=False) is False


def test_page_preflight_rejects_permission_for_different_page(monkeypatch):
    _configure(monkeypatch)
    with patch(
        "facebook.token_validator.requests.get",
        return_value=_response(_valid_debug(target_ids=["different-page"])),
    ):
        assert validate_token_or_exit(exit_on_failure=False) is False


def test_page_preflight_rejects_page_level_posting_restriction(monkeypatch):
    _configure(monkeypatch)
    page = {"id": "page-123", "name": "World Update", "is_published": True, "can_post": False}
    with patch(
        "facebook.token_validator.requests.get",
        side_effect=[_response(_valid_debug()), _response(page)],
    ):
        assert validate_token_or_exit(exit_on_failure=False) is False


def test_page_preflight_rejects_invalid_api_version(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(config, "FACEBOOK_GRAPH_API_VERSION", "latest")
    with patch("facebook.token_validator.requests.get") as get:
        assert validate_token_or_exit(exit_on_failure=False) is False
    get.assert_not_called()
