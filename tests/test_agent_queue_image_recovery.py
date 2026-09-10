import os
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("NEWSAPI_KEY", "test-key")

import pytest

from agent import (  # noqa: E402
    _assert_not_delivered,
    _replace_queue_image_with_fallback,
    _wait_for_immediate_spacing,
    publish_from_queue,
)
from models import DuplicateStory, Story, VerificationStatus  # noqa: E402
from pipeline.posting_queue import PostingQueue, QueueEntry  # noqa: E402
from pipeline.publish_receipts import record_publish_receipt  # noqa: E402


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


def test_later_immediate_post_waits_for_thirty_minute_gap():
    now = datetime.now(timezone.utc)
    with patch("agent.time.sleep") as sleep:
        waited = _wait_for_immediate_spacing(
            now.replace(microsecond=0),
            now=now.replace(microsecond=0),
        )
    assert waited == 1800
    sleep.assert_called_once_with(1800)


def test_final_delivery_guard_blocks_canonical_published_url(tmp_path):
    seen_path = tmp_path / "seen.json"
    seen_path.write_text(json.dumps({
        "urls": ["http://www.example.com/report?utm_source=facebook"],
        "titles": [],
        "url_attempts": {},
        "title_attempts": {},
    }), encoding="utf-8")
    story = Story(
        title="Same delivered report",
        source_name="Example",
        source_url="https://example.com/report?fbclid=abc",
        published_at=datetime.now(timezone.utc),
        raw_summary="Verified details.",
    )

    with patch("config.SEEN_STORIES_PATH", seen_path):
        with pytest.raises(DuplicateStory, match="Duplicate URL"):
            _assert_not_delivered(story)


def test_final_delivery_guard_blocks_existing_receipt_variant(tmp_path):
    seen_path = tmp_path / "seen.json"
    seen_path.write_text(json.dumps({
        "urls": [], "titles": [], "url_attempts": {}, "title_attempts": {},
    }), encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    record_publish_receipt(
        "http://www.example.com/report?utm_source=facebook",
        "Delivered report",
        "page_post_123",
        path=receipts,
    )
    story = Story(
        title="Delivered report from tracking link",
        source_name="Example",
        source_url="https://example.com/report?fbclid=abc",
        published_at=datetime.now(timezone.utc),
        raw_summary="Verified details.",
    )

    with (
        patch("config.SEEN_STORIES_PATH", seen_path),
        patch("pipeline.publish_receipts.RECEIPT_FILE", receipts),
    ):
        with pytest.raises(DuplicateStory, match="delivery receipt"):
            _assert_not_delivered(story)


def test_final_delivery_guard_uses_persistent_published_title_history(tmp_path):
    seen_path = tmp_path / "seen.json"
    seen_path.write_text(json.dumps({
        "urls": [], "titles": [], "url_attempts": {}, "title_attempts": {},
    }), encoding="utf-8")
    titles_path = tmp_path / "published_titles.json"
    titles_path.write_text(
        json.dumps(["Major earthquake strikes coastal region"]),
        encoding="utf-8",
    )
    receipts = tmp_path / "receipts.jsonl"
    story = Story(
        title="Major earthquake strikes coastal region today",
        source_name="Another Source",
        source_url="https://another.example/earthquake",
        published_at=datetime.now(timezone.utc),
        raw_summary="Verified details.",
    )

    with (
        patch("config.SEEN_STORIES_PATH", seen_path),
        patch("config.PUBLISHED_TITLES_PATH", titles_path),
        patch("pipeline.publish_receipts.RECEIPT_FILE", receipts),
    ):
        with pytest.raises(DuplicateStory, match="Similar title"):
            _assert_not_delivered(story)


def test_publisher_reconciles_receipt_without_calling_facebook(tmp_path):
    queue_path = tmp_path / "queue.json"
    audit_path = tmp_path / "audit.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    now = datetime.now(timezone.utc).isoformat()

    with (
        patch("pipeline.posting_queue.QUEUE_FILE", queue_path),
        patch("pipeline.posting_queue.AUDIT_FILE", audit_path),
    ):
        queue = PostingQueue()
        queue._entries = [QueueEntry(
            title="Already delivered report",
            source_name="Example",
            source_url="https://example.com/report?fbclid=queue",
            category="world",
            score=88,
            queued_at=now,
            source_published_at=now,
            verification_status=VerificationStatus.VERIFIED.value,
            status="QUEUED",
        )]
        queue._save()
        record_publish_receipt(
            "http://www.example.com/report?utm_source=facebook",
            "Already delivered report",
            "page_post_456",
            path=receipts,
        )

        with (
            patch("pipeline.publish_receipts.RECEIPT_FILE", receipts),
            patch("facebook.token_validator.validate_token_or_exit"),
            patch("facebook.publisher.publish_post_with_image") as publish,
            patch("agent._write_run_summary"),
        ):
            assert publish_from_queue() == 0

        publish.assert_not_called()
        reloaded = PostingQueue()
        assert reloaded._entries[0].status == "PUBLISHED"
        assert reloaded._entries[0].facebook_post_id == "page_post_456"
