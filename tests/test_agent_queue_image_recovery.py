import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("NEWSAPI_KEY", "test-key")

from agent import _replace_queue_image_with_fallback, _wait_for_immediate_spacing  # noqa: E402
from models import Story  # noqa: E402


def test_publish_recovery_uses_full_image_chain_before_branded_fallback(tmp_path):
    recovered_path = tmp_path / "story.jpg"
    recovered_path.write_bytes(b"card")
    story = Story(
        title="Verified story",
        source_name="Reuters",
        source_url="https://reuters.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="A verified update.",
    )
    entry = SimpleNamespace(
        image_path="missing.jpg",
        image_provenance="legacy_unknown",
        image_credit="",
        image_is_synthetic=False,
    )
    queue = SimpleNamespace(_save=Mock())

    def create_image(current_story):
        current_story.image_provenance = "pexels"
        current_story.image_credit = "Licensed Photographer"
        current_story.image_is_synthetic = False
        return recovered_path

    with (
        patch("image.maker.create_news_image", side_effect=create_image),
        patch("image.maker.create_fallback_card") as fallback,
    ):
        result = _replace_queue_image_with_fallback(queue, entry, story)

    assert result == recovered_path
    assert entry.image_path == str(recovered_path)
    assert entry.image_provenance == "pexels"
    assert entry.image_credit == "Licensed Photographer"
    fallback.assert_not_called()
    queue._save.assert_called_once_with()


def test_first_immediate_post_has_no_spacing_delay():
    with patch("agent.time.sleep") as sleep:
        assert _wait_for_immediate_spacing(None) == 0
    sleep.assert_not_called()


def test_later_immediate_post_waits_for_ten_minute_gap():
    now = datetime.now(timezone.utc)
    with patch("agent.time.sleep") as sleep:
        waited = _wait_for_immediate_spacing(
            now.replace(microsecond=0),
            now=now.replace(microsecond=0),
        )
    assert waited == 600
    sleep.assert_called_once_with(600)
