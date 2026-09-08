"""
pipeline/source_health.py — Track RSS feed health and ban unreliable sources.

Rules:
  - A transient failure is recorded but the source remains available next run
  - Three consecutive failures trigger a one-hour cool-off
  - One successful response clears the failure streak
  - Health records are persisted in source_health.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

HEALTH_FILE         = config.SOURCE_HEALTH_PATH
BAN_DURATION_H      = 1      # short cool-off after repeated failures
FAILURE_THRESHOLD   = 3      # a single transient failure must not remove a source
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
    if not entry.get("banned_until"):
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


def record_failure(feed_url: str, reason: str) -> None:
    """Record a failure and cool off only after repeated consecutive failures."""
    data = _load()
    previous = data.get(feed_url, {})
    count = int(previous.get("consecutive_failures", 0)) + 1
    entry = {
        "consecutive_failures": count,
        "last_failure": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    if count >= FAILURE_THRESHOLD:
        entry["banned_at"] = datetime.now(timezone.utc).isoformat()
        entry["banned_until"] = (
            datetime.now(timezone.utc) + timedelta(hours=BAN_DURATION_H)
        ).isoformat()
        logger.warning("⛔ Source cooling off after %d failures — %s", count, feed_url)
    else:
        logger.warning("Source failure %d/%d — %s", count, FAILURE_THRESHOLD, feed_url)
    data[feed_url] = entry
    _save(data)


def record_success(feed_url: str) -> None:
    """Clear a source's transient failure streak after a successful response."""
    data = _load()
    if feed_url in data:
        del data[feed_url]
        _save(data)


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
