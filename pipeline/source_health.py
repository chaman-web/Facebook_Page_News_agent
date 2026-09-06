"""
pipeline/source_health.py — Track RSS feed health and ban unreliable sources.

Rules:
  - If a feed returns >= DUPLICATE_THRESHOLD duplicates in one fetch → ban 12h
  - If a feed fails (connection error, timeout, parse error) → ban 12h
  - After 12h ban expires → source is tried again automatically
  - Ban records are persisted in source_health.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HEALTH_FILE         = Path("source_health.json")
BAN_DURATION_H      = 12     # hours to ban a failing source
DUPLICATE_THRESHOLD = 0.8    # ban if 80%+ of stories are duplicates

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def is_banned(feed_url: str) -> bool:
    """Return True if this feed is currently banned."""
    data  = _load()
    entry = data.get(feed_url)
    if not entry:
        return False
    banned_until = datetime.fromisoformat(entry["banned_until"])
    if datetime.now(timezone.utc) < banned_until:
        remaining = int((banned_until - datetime.now(timezone.utc)).total_seconds() / 60)
        logger.debug("Source banned (%dm remaining): %s", remaining, feed_url)
        return True
    # Ban expired — clean up
    del data[feed_url]
    _save(data)
    return False


def ban(feed_url: str, reason: str) -> None:
    """Ban a feed for BAN_DURATION_H hours."""
    data  = _load()
    until = datetime.now(timezone.utc) + timedelta(hours=BAN_DURATION_H)
    data[feed_url] = {
        "banned_until": until.isoformat(),
        "reason":       reason,
        "banned_at":    datetime.now(timezone.utc).isoformat(),
    }
    _save(data)
    logger.warning("⛔ Source banned for %dh — %s | Reason: %s", BAN_DURATION_H, feed_url, reason)


def record_duplicates(feed_url: str, total: int, duplicates: int) -> None:
    """Ban a feed if too many of its stories are duplicates."""
    if total == 0:
        return
    ratio = duplicates / total
    if ratio >= DUPLICATE_THRESHOLD:
        ban(feed_url, f"High duplicate ratio {ratio:.0%} ({duplicates}/{total})")


def get_status() -> list[dict]:
    """Return all currently active bans with time remaining."""
    data = _load()
    now  = datetime.now(timezone.utc)
    active = []
    expired = []
    for url, entry in data.items():
        until = datetime.fromisoformat(entry["banned_until"])
        if now < until:
            mins = int((until - now).total_seconds() / 60)
            active.append({"url": url, "reason": entry["reason"], "minutes_remaining": mins})
        else:
            expired.append(url)
    # Clean expired bans
    if expired:
        for url in expired:
            del data[url]
        _save(data)
    return active


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load() -> dict:
    if not HEALTH_FILE.exists():
        return {}
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    HEALTH_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
