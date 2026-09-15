"""
pipeline/error_log.py — Structured error logging for the pipeline.

Writes every unhandled exception and critical failure to logs/errors.log
as a JSON-lines record so errors are easy to inspect and track over time.

Each record contains:
  timestamp   — when the error occurred (ISO 8601)
  run_id      — pipeline run that produced it
  job         — fetch | publish | scheduler
  stage       — pipeline stage where it happened
  error_type  — exception class name
  message     — str(exc)
  traceback   — full traceback string

Log is retained for the same 2-day window as other pipeline logs.
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from pipeline.log_retention import prune_log_file

logger = logging.getLogger(__name__)

ERROR_LOG_PATH = config.LOGS_DIR / "errors.log"


def write_error(
    exc: BaseException,
    *,
    job: str,
    stage: str,
    run_id: str = "",
    extra: Optional[dict] = None,
) -> Path:
    """
    Append a structured error record to logs/errors.log.

    Usage:
        from pipeline.error_log import write_error
        try:
            risky_operation()
        except Exception as exc:
            write_error(exc, job="fetch", stage="Stage 3 — Verify Sources")
    """
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Prune entries older than 2 days on every write
    prune_log_file(ERROR_LOG_PATH)

    now = datetime.now().astimezone()
    record: dict = {
        "timestamp":  now.isoformat(),
        "run_id":     run_id,
        "job":        job,
        "stage":      stage,
        "error_type": type(exc).__name__,
        "message":    str(exc),
        "traceback":  traceback.format_exc(),
    }
    if extra:
        record.update(extra)

    with ERROR_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.error(
        "Error logged → logs/errors.log | job=%s | stage=%s | %s: %s",
        job, stage, type(exc).__name__, str(exc)[:120],
    )
    return ERROR_LOG_PATH


def read_recent_errors(n: int = 20) -> list[dict]:
    """Return the last n error records from errors.log."""
    if not ERROR_LOG_PATH.exists():
        return []
    records = []
    for line in ERROR_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records[-n:]
