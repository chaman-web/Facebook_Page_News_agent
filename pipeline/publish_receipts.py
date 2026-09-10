"""Durable Facebook delivery receipts used to prevent duplicate posts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import config
from pipeline.deduplicator import canonical_story_url


RECEIPT_FILE = config.PUBLISH_RECEIPTS_PATH


def record_publish_receipt(
    source_url: str,
    title: str,
    post_id: str,
    *,
    published_at: str | None = None,
    path: Path | None = None,
) -> dict:
    """Persist API success before mutable queue bookkeeping begins."""
    receipt = {
        "source_url": source_url,
        "canonical_url": canonical_story_url(source_url),
        "title": title[:160],
        "post_id": post_id,
        "published_at": published_at or datetime.now(timezone.utc).isoformat(),
    }
    target = path or RECEIPT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return receipt


def load_publish_receipts(path: Path | None = None) -> dict[str, dict]:
    """Return the latest valid receipt for each source URL."""
    target = path or RECEIPT_FILE
    if not target.exists():
        return {}
    receipts: dict[str, dict] = {}
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError:
            continue
        source_url = receipt.get("canonical_url") or receipt.get("source_url")
        if source_url and receipt.get("post_id"):
            receipts[canonical_story_url(source_url)] = receipt
    return receipts


def reconcile_publish_receipts(queue, path: Path | None = None) -> int:
    """Mark queued API-success entries published without sending them again."""
    receipts = load_publish_receipts(path)
    changed = 0
    for entry in queue._entries:
        receipt = receipts.get(canonical_story_url(entry.source_url))
        if entry.status != "QUEUED" or not receipt:
            continue
        entry.status = "PUBLISHED"
        entry.facebook_post_id = receipt["post_id"]
        entry.published_at = receipt.get("published_at") or datetime.now(timezone.utc).isoformat()
        entry.next_retry_at = None
        entry.last_publish_error = ""
        changed += 1
    if changed:
        queue._save()
    return changed


def find_publish_receipt(source_url: str, path: Path | None = None) -> dict | None:
    """Find a prior Facebook delivery across equivalent URL variants."""
    return load_publish_receipts(path).get(canonical_story_url(source_url))
