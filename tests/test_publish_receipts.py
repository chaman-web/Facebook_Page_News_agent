from datetime import datetime, timezone
from unittest.mock import Mock

from pipeline.posting_queue import QueueEntry
from pipeline.publish_receipts import (
    load_publish_receipts,
    reconcile_publish_receipts,
    record_publish_receipt,
)


def test_receipt_is_durable_and_reconciles_queued_entry(tmp_path):
    path = tmp_path / "receipts.jsonl"
    published_at = datetime.now(timezone.utc).isoformat()
    record_publish_receipt(
        "https://example.com/story",
        "Verified story",
        "page_post_123",
        published_at=published_at,
        path=path,
    )
    entry = QueueEntry(
        "Verified story", "Example", "https://example.com/story", "world", 75,
        published_at, status="QUEUED",
    )
    queue = Mock()
    queue._entries = [entry]

    assert load_publish_receipts(path)[entry.source_url]["post_id"] == "page_post_123"
    assert reconcile_publish_receipts(queue, path) == 1
    assert entry.status == "PUBLISHED"
    assert entry.facebook_post_id == "page_post_123"
    assert entry.published_at == published_at
    queue._save.assert_called_once_with()


def test_malformed_receipt_line_is_ignored(tmp_path):
    path = tmp_path / "receipts.jsonl"
    path.write_text(
        '{"source_url":"https://bad.test"\n'
        '{"source_url":"https://good.test","post_id":"post_9"}\n',
        encoding="utf-8",
    )

    assert load_publish_receipts(path) == {
        "https://good.test": {
            "source_url": "https://good.test",
            "post_id": "post_9",
        }
    }
