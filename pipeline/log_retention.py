"""Keep scheduled-job text logs limited to the two latest local dates."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_RETENTION_DAYS = 2
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})\s")


def retained_log_handler(path: Path) -> TimedRotatingFileHandler:
    """Prune an active log and return a midnight rotating UTF-8 handler."""
    path.parent.mkdir(parents=True, exist_ok=True)
    prune_log_file(path)
    prune_rotated_logs(path)
    return TimedRotatingFileHandler(
        path,
        when="midnight",
        interval=1,
        backupCount=LOG_RETENTION_DAYS - 1,
        encoding="utf-8",
        delay=True,
    )


def prune_log_file(
    path: Path,
    *,
    today: date | None = None,
    retention_days: int = LOG_RETENTION_DAYS,
) -> int:
    """Remove timestamped log blocks older than the retention window."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    if not path.exists():
        return 0

    local_today = today or datetime.now().astimezone().date()
    cutoff = local_today - timedelta(days=retention_days - 1)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    kept: list[str] = []
    keep_block = False
    removed = 0

    for line in lines:
        match = _TIMESTAMP.match(line)
        if match:
            try:
                keep_block = date.fromisoformat(match.group(1)) >= cutoff
            except ValueError:
                keep_block = False
        if keep_block:
            kept.append(line)
        else:
            removed += 1

    if removed:
        temporary = path.with_name(f"{path.name}.retention.tmp")
        temporary.write_text("".join(kept), encoding="utf-8")
        temporary.replace(path)
    return removed


def prune_rotated_logs(
    path: Path,
    *,
    today: date | None = None,
    retention_days: int = LOG_RETENTION_DAYS,
) -> int:
    """Remove dated rollover files outside the same two-day window."""
    local_today = today or datetime.now().astimezone().date()
    cutoff = local_today - timedelta(days=retention_days - 1)
    removed = 0
    prefix = f"{path.name}."
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name[len(prefix):]
        try:
            log_date = date.fromisoformat(suffix[:10])
        except ValueError:
            continue
        if log_date < cutoff:
            candidate.unlink()
            removed += 1
    return removed
