"""
schedule_monitor.py — Detect and log late scheduled job triggers.

Called at the top of each batch file before the pipeline runs.
Compares the current time to the nearest expected trigger window and logs
a warning to logs/schedule_warnings.log if the job fired late.

Usage:
    python pipeline/schedule_monitor.py --job fetch
    python pipeline/schedule_monitor.py --job publish

Exit code is always 0 so a late warning never blocks the pipeline.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

WARNING_LOG = LOGS_DIR / "schedule_warnings.log"

# How many minutes past the expected time counts as "late"
LATE_THRESHOLD_MINUTES = 15

# Expected trigger times for each job (local 24h)
JOB_WINDOWS = {
    "fetch":   ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"],
    "publish": [],  # publish is event-driven, no fixed windows — skip check
}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

file_handler = logging.FileHandler(WARNING_LOG, encoding="utf-8")
file_handler.setLevel(logging.WARNING)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def nearest_window(now: datetime, windows: list[str]) -> tuple[datetime, str]:
    """Return the (datetime, label) of the most recently expected trigger."""
    today = now.date()
    candidates: list[datetime] = []

    for w in windows:
        h, m = map(int, w.split(":"))
        candidates.append(datetime(today.year, today.month, today.day, h, m))
        yesterday = today - timedelta(days=1)
        candidates.append(datetime(yesterday.year, yesterday.month, yesterday.day, h, m))

    past = [c for c in candidates if c <= now]
    if not past:
        return now, "unknown"

    nearest = max(past)
    label = nearest.strftime("%H:%M")
    return nearest, label


def check_lateness(job: str, _now: datetime | None = None) -> None:
    """Check if the job fired late and log a warning if so.

    Args:
        job:  'fetch' or 'publish'
        _now: override current time (for testing only)
    """
    windows = JOB_WINDOWS.get(job, [])

    if not windows:
        logger.info("Job '%s' has no fixed windows — skipping lateness check.", job)
        return

    now = _now if _now is not None else datetime.now()
    expected, label = nearest_window(now, windows)
    delta_minutes = (now - expected).total_seconds() / 60

    if delta_minutes > LATE_THRESHOLD_MINUTES:
        msg = (
            f"Job '{job}' triggered {int(delta_minutes)} minutes late "
            f"(expected {label}, ran at {now.strftime('%H:%M')}). "
            f"Machine was likely asleep or task scheduler was delayed."
        )
        logger.warning(msg)

        record = {
            "timestamp": now.isoformat(),
            "job": job,
            "expected_window": label,
            "actual_time": now.strftime("%H:%M"),
            "delay_minutes": round(delta_minutes, 1),
            "reason": "Machine sleep or task scheduler delay suspected",
        }
        late_log = LOGS_DIR / "late_triggers.jsonl"
        with late_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        print(
            f"⚠️  WARNING: Job '{job}' ran {int(delta_minutes)} min late "
            f"(expected {label}). Logged to logs/schedule_warnings.log"
        )
    else:
        logger.info(
            "Job '%s' on time — expected %s, ran at %s (%.1f min delta).",
            job, label, now.strftime("%H:%M"), delta_minutes,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Schedule lateness monitor")
    parser.add_argument("--job", required=True, choices=["fetch", "publish"],
                        help="Which job is starting")
    args = parser.parse_args()
    check_lateness(args.job)
    sys.exit(0)  # always exit 0 — never block the pipeline


if __name__ == "__main__":
    main()
