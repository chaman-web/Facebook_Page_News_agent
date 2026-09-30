"""Append-only recommendation and manual-submission ledger."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def load_latest(path: Path) -> dict[tuple[str, str], dict]:
    latest: dict[tuple[str, str], dict] = {}
    if not path.exists(): return latest
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try: record = json.loads(line)
        except json.JSONDecodeError: continue
        key = (str(record.get("facebook_post_id", "")), str(record.get("group_id", "")))
        if all(key): latest[key] = record
    return latest


def append(path: Path, record: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    item = dict(record)
    item.setdefault("recommended_at", datetime.now(timezone.utc).isoformat())
    item.setdefault("confirmed_shared_at", "")
    item.setdefault("delivery_status", "RECOMMENDED")
    item.setdefault("last_error", "")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        handle.flush(); os.fsync(handle.fileno())
    return item


def record_manual_submission(
    path: Path,
    facebook_post_id: str,
    group_id: str,
    *,
    pending_admin: bool = True,
) -> dict:
    """Record an operator submission; this function never performs the share."""
    current = load_latest(path).get((facebook_post_id, group_id))
    if current is None:
        raise KeyError("No recommendation exists for this Facebook post and Group.")
    status = "SUBMITTED_PENDING_ADMIN" if pending_admin else "SUBMITTED"
    return append(path, {**current, "delivery_status": status, "recommendation_status": status})
