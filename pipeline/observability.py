"""Run identifiers and compact two-day operational summaries."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import config

SUMMARY_RETENTION_DAYS = 2


def new_run_id(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return f"{current:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


RUN_ID = os.environ.get("GLOBAL_PULSE_RUN_ID") or new_run_id()


class RunIdFilter(logging.Filter):
    """Attach one stable run identifier to every logging record."""

    def __init__(self, run_id: str = RUN_ID) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def add_run_id(handler: logging.Handler, run_id: str = RUN_ID) -> logging.Handler:
    handler.addFilter(RunIdFilter(run_id))
    return handler


def write_daily_summary(
    job: str,
    status: str,
    metrics: dict,
    *,
    run_id: str = RUN_ID,
    now: datetime | None = None,
    directory: Path | None = None,
) -> Path:
    """Append one compact JSON record and retain only two local daily files."""
    current = now or datetime.now().astimezone()
    target_dir = directory or config.LOGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    _prune_summary_files(target_dir, today=current.date())
    path = target_dir / f"daily_summary_{current:%Y-%m-%d}.jsonl"
    record = {
        "timestamp": current.isoformat(),
        "run_id": run_id,
        "job": job,
        "status": status,
        **metrics,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _prune_summary_files(directory: Path, *, today) -> int:
    cutoff = today - timedelta(days=SUMMARY_RETENTION_DAYS - 1)
    removed = 0
    for path in directory.glob("daily_summary_*.jsonl"):
        try:
            file_date = datetime.strptime(path.stem.removeprefix("daily_summary_"), "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink()
            removed += 1
    return removed
