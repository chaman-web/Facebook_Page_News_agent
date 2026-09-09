import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("NEWSAPI_KEY", "test-key")

from agent import _replace_queue_image_with_fallback  # noqa: E402
from models import Story  # noqa: E402


def test_publish_recovery_replaces_missing_queue_image_with_branded_fallback(tmp_path):
    fallback_path = tmp_path / "story_fallback.jpg"
    fallback_path.write_bytes(b"card")
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

    def create_fallback(current_story):
        current_story.image_provenance = "branded_fallback"
        current_story.image_credit = "Global Pulse News"
        current_story.image_is_synthetic = False
        return fallback_path

    with patch("image.maker.create_fallback_card", side_effect=create_fallback):
        result = _replace_queue_image_with_fallback(queue, entry, story)

    assert result == fallback_path
    assert entry.image_path == str(fallback_path)
    assert entry.image_provenance == "branded_fallback"
    assert entry.image_credit == "Global Pulse News"
    queue._save.assert_called_once_with()
