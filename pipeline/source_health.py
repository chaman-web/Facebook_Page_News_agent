"""Adaptive RSS endpoint health tracking with automatic recovery probes.

Cooling is temporary and applies to one feed URL only. A publisher, domain,
story, or editorial score is never blocked because one endpoint is unhealthy.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import config
from pipeline.source_classifier import SourceTier, classify_source

logger = logging.getLogger(__name__)

HEALTH_FILE = config.SOURCE_HEALTH_PATH
FAILURE_THRESHOLD = 4
IMPORTANT_FAILURE_THRESHOLD = 5
DUPLICATE_THRESHOLD = 0.8

# Progressive temporary cooldowns. Nothing is permanently disabled.
COOLDOWN_MINUTES = (5, 15, 60, 360, 1440)
IMPORTANT_COOLDOWN_CAP_MINUTES = 360


def is_banned(feed_url: str) -> bool:
    """Return whether an endpoint is cooling, allowing periodic recovery probes."""
    data = _load()
    entry = data.get(feed_url)
    if not entry or not entry.get("banned_until"):
        return False

    now = _utcnow()
    until = _parse_time(entry.get("banned_until"))
    if until is None or now >= until:
        _clear_cooldown(entry)
        entry["status"] = "RETRYING"
        data[feed_url] = entry
        _save(data)
        logger.info("Source cooldown expired; retrying endpoint — %s", feed_url)
        return False

    next_probe = _parse_time(entry.get("next_probe_at"))
    if next_probe is None or now >= next_probe:
        interval = _probe_interval_minutes(feed_url, entry)
        entry["last_probe_at"] = now.isoformat()
        entry["next_probe_at"] = min(until, now + timedelta(minutes=interval)).isoformat()
        entry["status"] = "RECOVERY_PROBE"
        data[feed_url] = entry
        _save(data)
        logger.info(
            "Recovery probe allowed during cooldown — %s | failures=%d | type=%s",
            feed_url,
            int(entry.get("consecutive_failures", 0)),
            entry.get("failure_type", "unknown"),
        )
        return False

    remaining = max(1, int((until - now).total_seconds() / 60))
    logger.debug(
        "Source endpoint cooling (%dm remaining; next probe %s): %s",
        remaining,
        entry.get("next_probe_at", "pending"),
        feed_url,
    )
    return True


def ban(feed_url: str, reason: str) -> None:
    """Temporarily cool an endpoint; retained for compatibility with old callers."""
    data = _load()
    previous = data.get(feed_url, {})
    failure_type = _classify_failure(reason)
    count = max(
        _failure_threshold(feed_url, failure_type),
        int(previous.get("consecutive_failures", 0)) + 1,
    )
    entry = _failure_entry(previous, reason, failure_type, count)
    _start_cooldown(feed_url, entry)
    data[feed_url] = entry
    _save(data)


def record_failure(feed_url: str, reason: str) -> None:
    """Record an endpoint failure and apply an adaptive temporary cooldown."""
    data = _load()
    previous = data.get(feed_url, {})
    failure_type = _classify_failure(reason)
    count = int(previous.get("consecutive_failures", 0)) + 1
    entry = _failure_entry(previous, reason, failure_type, count)
    threshold = _failure_threshold(feed_url, failure_type)

    if count >= threshold:
        _start_cooldown(feed_url, entry)
    else:
        entry["status"] = "DEGRADED"
        logger.warning(
            "Source endpoint failure %d/%d — %s | type=%s | %s",
            count,
            threshold,
            feed_url,
            failure_type,
            reason,
        )

    data[feed_url] = entry
    _save(data)


def record_success(feed_url: str) -> None:
    """Restore an endpoint immediately after one successful response."""
    data = _load()
    previous = data.get(feed_url, {})
    was_degraded = bool(previous.get("consecutive_failures") or previous.get("banned_until"))
    entry = dict(previous)
    _clear_cooldown(entry)
    entry.update(
        {
            "status": "HEALTHY",
            "consecutive_failures": 0,
            "cooldown_level": 0,
            "last_success": _utcnow().isoformat(),
            "total_successes": int(previous.get("total_successes", 0)) + 1,
        }
    )
    data[feed_url] = entry
    _save(data)
    if was_degraded:
        logger.info("✅ Source endpoint recovered automatically — %s", feed_url)


def record_duplicates(feed_url: str, total: int, duplicates: int) -> None:
    """Record duplicate volume as a quality signal without cooling the endpoint."""
    if total <= 0:
        return
    ratio = duplicates / total
    data = _load()
    entry = dict(data.get(feed_url, {}))
    entry["last_duplicate_ratio"] = round(ratio, 4)
    entry["last_duplicate_check"] = _utcnow().isoformat()
    data[feed_url] = entry
    _save(data)
    if ratio >= DUPLICATE_THRESHOLD:
        logger.info(
            "High duplicate ratio recorded without cooling source — %s | %.0f%% (%d/%d)",
            feed_url,
            ratio * 100,
            duplicates,
            total,
        )


def get_status() -> list[dict]:
    """Return active endpoint cooldowns with recovery details."""
    data = _load()
    now = _utcnow()
    active = []
    changed = False
    for url, entry in data.items():
        until = _parse_time(entry.get("banned_until"))
        if until is None:
            continue
        if now >= until:
            _clear_cooldown(entry)
            entry["status"] = "RETRYING"
            changed = True
            continue
        active.append(
            {
                "url": url,
                "reason": entry.get("reason", "unknown"),
                "failure_type": entry.get("failure_type", "unknown"),
                "consecutive_failures": int(entry.get("consecutive_failures", 0)),
                "minutes_remaining": max(1, int((until - now).total_seconds() / 60)),
                "next_probe_at": entry.get("next_probe_at"),
                "status": entry.get("status", "COOLING"),
            }
        )
    if changed:
        _save(data)
    return active


def _failure_entry(previous: dict, reason: str, failure_type: str, count: int) -> dict:
    entry = dict(previous)
    entry.update(
        {
            "consecutive_failures": count,
            "last_failure": _utcnow().isoformat(),
            "reason": reason,
            "failure_type": failure_type,
            "total_failures": int(previous.get("total_failures", 0)) + 1,
        }
    )
    return entry


def _start_cooldown(feed_url: str, entry: dict) -> None:
    now = _utcnow()
    level = min(int(entry.get("cooldown_level", 0)) + 1, len(COOLDOWN_MINUTES))
    duration = COOLDOWN_MINUTES[level - 1]
    if _is_important(feed_url):
        duration = min(duration, IMPORTANT_COOLDOWN_CAP_MINUTES)
    probe_in = min(
        duration,
        5 if _is_important(feed_url) else max(5, min(60, duration // 3)),
    )
    entry.update(
        {
            "status": "COOLING",
            "cooldown_level": level,
            "banned_at": now.isoformat(),
            "banned_until": (now + timedelta(minutes=duration)).isoformat(),
            "next_probe_at": (now + timedelta(minutes=probe_in)).isoformat(),
        }
    )
    logger.warning(
        "⏳ Source endpoint cooling temporarily — %s | type=%s | failures=%d | cooldown=%dm | probe=%dm",
        feed_url,
        entry.get("failure_type", "unknown"),
        int(entry.get("consecutive_failures", 0)),
        duration,
        probe_in,
    )
    if _is_important(feed_url) and duration >= 360:
        logger.error(
            "SOURCE COVERAGE ALERT: important endpoint has a prolonged outage; "
            "alternative feeds and cached discovery remain active — %s",
            feed_url,
        )


def _failure_threshold(feed_url: str, failure_type: str) -> int:
    if failure_type == "rate_limit":
        return 2
    if failure_type in {"not_found", "invalid_feed"}:
        return 3 if _is_important(feed_url) else 2
    return IMPORTANT_FAILURE_THRESHOLD if _is_important(feed_url) else FAILURE_THRESHOLD


def _probe_interval_minutes(feed_url: str, entry: dict) -> int:
    if _is_important(feed_url):
        return 5
    level = max(1, int(entry.get("cooldown_level", 1)))
    duration = COOLDOWN_MINUTES[min(level - 1, len(COOLDOWN_MINUTES) - 1)]
    return max(5, min(60, duration // 3))


def _is_important(feed_url: str) -> bool:
    return classify_source("", feed_url).tier <= SourceTier.TIER2


def _classify_failure(reason: str) -> str:
    text = (reason or "").lower()
    if "429" in text or "rate limit" in text:
        return "rate_limit"
    if "404" in text or "410" in text or "not found" in text:
        return "not_found"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "nameresolution" in text or "name resolution" in text or "dns" in text:
        return "dns"
    if any(code in text for code in ("500", "502", "503", "504")):
        return "server_error"
    if "connection" in text or "network" in text:
        return "connection"
    if "parse" in text or "xml" in text or "malformed" in text:
        return "invalid_feed"
    return "other"


def _clear_cooldown(entry: dict) -> None:
    for key in ("banned_at", "banned_until", "next_probe_at", "last_probe_at"):
        entry.pop(key, None)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict:
    if not HEALTH_FILE.exists():
        return {}
    try:
        value = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
