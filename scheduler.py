"""
scheduler.py — Automated publishing for Global Pulse News.

Publishing philosophy:
  - Quality over volume. Never post weak stories to hit a quota.
  - Normal days: 4–6 strong posts spread across the day.
  - Busy news days: up to 10 posts when genuinely important stories exist.
  - Breaking news is never delayed by schedule intervals.

Publishing windows (local time):
  00:00 — Global evening window
  13:00 — Lunch window
  18:00 — Evening window

At each window, publishes 1 story per category across all 14 categories.
The deduplicator and quality gate filter this down to the best stories.

Health check:
  - Before each window, the token is validated via a fast /me API call.
  - If a previous agent run is still active when the next window fires,
    the new window is skipped to prevent duplicate concurrent publishes.
  - Each run is logged to run_log.jsonl (start, end, published, failed, stop reason).

Usage:
    python scheduler.py               # run with images (default)
    python scheduler.py --no-image    # text only
    python scheduler.py --count 2     # 2 posts per category per window
    python scheduler.py --dry-run     # preview without publishing
    python scheduler.py --times 00:00 13:00 18:00   # custom windows

Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import schedule

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_PEAK_TIMES = config.PUBLISH_WINDOWS_LOCAL
RUN_LOG_FILE       = config.RUN_LOG_PATH

# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------

_run_lock       = threading.Lock()
_active_process: subprocess.Popen | None = None
_active_process_lock = threading.Lock()


def _is_run_active() -> bool:
    """Return True if an agent subprocess is currently running."""
    with _active_process_lock:
        if _active_process is None:
            return False
        poll = _active_process.poll()
        return poll is None   # None means still running


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

def _log_run(
    window:    str,
    start:     datetime,
    end:       datetime,
    published: int,
    failed:    int,
    skipped:   int,
    exit_code: int,
    stop_reason: str,
) -> None:
    record = {
        "window":      window,
        "start":       start.isoformat(),
        "end":         end.isoformat(),
        "duration_s":  int((end - start).total_seconds()),
        "published":   published,
        "failed":      failed,
        "skipped":     skipped,
        "exit_code":   exit_code,
        "stop_reason": stop_reason,
    }
    try:
        with RUN_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("Run log write failed: %s", exc)


def _parse_run_stats(output: str) -> tuple[int, int, int]:
    """
    Parse agent stdout for published/failed/skipped counts.
    Returns (published, failed, skipped).
    These are best-effort — defaults to 0 if not parseable.
    """
    import re
    published = failed = skipped = 0
    for line in output.splitlines():
        m = re.search(r"Published.*?(\d+).*?post", line, re.I)
        if m:
            published = int(m.group(1))
        m = re.search(r"Failed.*?(\d+)", line, re.I)
        if m:
            failed = int(m.group(1))
        m = re.search(r"Skipped.*?(\d+)", line, re.I)
        if m:
            skipped = int(m.group(1))
    return published, failed, skipped


# ---------------------------------------------------------------------------
# Token pre-validation
# ---------------------------------------------------------------------------

def _validate_token_quick() -> bool:
    """
    Quick token check before starting a publishing window.
    Runs the token validator in a subprocess to avoid import-time side effects.
    Returns True if the token appears valid.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "from facebook.token_validator import validate_token_or_exit; "
             "validate_token_or_exit(exit_on_failure=False) or exit(1)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            # Print the validation error from the subprocess
            for line in (result.stderr + result.stdout).splitlines():
                if line.strip():
                    logger.error("  %s", line)
            return False
        # Log any info lines from the validator
        for line in result.stdout.splitlines():
            if "✅" in line or "Token" in line:
                logger.info("  %s", line.strip())
        return True
    except Exception as exc:
        logger.warning("Token pre-check failed (%s) — proceeding anyway.", exc)
        return True   # don't block the run on a validation failure


# ---------------------------------------------------------------------------
# Core window runner
# ---------------------------------------------------------------------------

def _run_window(window_label: str, count: int, image: bool, dry_run: bool) -> None:
    """Launch agent.py for one posting window with health check and run log."""
    global _active_process

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Health check: skip if a previous window is still running
    if _is_run_active():
        logger.warning(
            "⚠️  Window %s skipped — previous run is still active. "
            "Agent may be slow (LLM or network). Will retry at next window.",
            window_label,
        )
        _log_run(
            window=window_label,
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc),
            published=0, failed=0, skipped=0,
            exit_code=-1,
            stop_reason="SKIPPED: previous run still active",
        )
        return

    # Token pre-validation (#4)
    if not dry_run:
        logger.info("⏰ Window %s — validating token...", window_label)
        if not _validate_token_quick():
            logger.error(
                "🔴 Token invalid — skipping window %s.\n"
                "   Update FACEBOOK_PAGE_TOKEN in .env and restart the scheduler.",
                window_label,
            )
            _log_run(
                window=window_label,
                start=datetime.now(timezone.utc),
                end=datetime.now(timezone.utc),
                published=0, failed=0, skipped=0,
                exit_code=-1,
                stop_reason="SKIPPED: token validation failed",
            )
            return

    logger.info(
        "⏰ Window %s (%s) — publishing all 14 categories (%d per cat)...",
        window_label, now_str, count,
    )

    cmd = [sys.executable, str(config.PROJECT_ROOT / "agent.py"), "--all-categories", f"--count={count}"]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd.append("--publish")
    if image and not dry_run:
        cmd.append("--image")

    logger.info("  cmd: %s", " ".join(cmd))

    start_time = datetime.now(timezone.utc)
    published = failed = skipped = 0
    exit_code  = -1
    stop_reason = "COMPLETED"

    try:
        with _active_process_lock:
            _active_process = subprocess.Popen(
                cmd,
                cwd=config.PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        # Stream output line-by-line
        output_lines: list[str] = []
        if _active_process.stdout:
            for line in _active_process.stdout:
                line = line.rstrip()
                output_lines.append(line)
                logger.info("  | %s", line)

        _active_process.wait()
        exit_code = _active_process.returncode
        full_output = "\n".join(output_lines)
        published, failed, skipped = _parse_run_stats(full_output)

        if exit_code == 0:
            logger.info(
                "✅ Window %s complete — %d published, %d failed, %d skipped.",
                window_label, published, failed, skipped,
            )
        elif exit_code == 1:
            stop_reason = "TOKEN_ERROR"
            logger.error(
                "🔴 Window %s — agent stopped due to token error (exit %d). "
                "Refresh FACEBOOK_PAGE_TOKEN in .env.",
                window_label, exit_code,
            )
        else:
            stop_reason = f"EXIT_{exit_code}"
            logger.error(
                "❌ Window %s — agent exited with code %d.",
                window_label, exit_code,
            )

    except Exception as exc:
        stop_reason = f"EXCEPTION: {exc}"
        logger.error("❌ Window %s — unexpected error: %s", window_label, exc)
    finally:
        with _active_process_lock:
            _active_process = None
        end_time = datetime.now(timezone.utc)
        duration = int((end_time - start_time).total_seconds())
        logger.info(
            "  Window %s finished in %dm %ds.",
            window_label, duration // 60, duration % 60,
        )
        _log_run(
            window=window_label,
            start=start_time,
            end=end_time,
            published=published,
            failed=failed,
            skipped=skipped,
            exit_code=exit_code,
            stop_reason=stop_reason,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Global Pulse News — Scheduled Facebook Publisher"
    )
    parser.add_argument("--no-image", action="store_true", help="Skip image generation")
    parser.add_argument("--count",    type=int, default=1,  help="Posts per category per window")
    parser.add_argument("--dry-run",  action="store_true",  help="Dry run — no publishing")
    parser.add_argument(
        "--times", nargs="+", default=DEFAULT_PEAK_TIMES,
        metavar="HH:MM", help="Posting window times (24h, space-separated)"
    )
    args = parser.parse_args()

    use_image = not args.no_image
    count     = args.count
    dry_run   = args.dry_run
    times     = args.times

    mode = "DRY RUN" if dry_run else ("IMAGE" if use_image else "TEXT-ONLY")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Global Pulse News — Scheduler")
    logger.info("  Mode     : %s", mode)
    logger.info("  Posts    : %d per category per window", count)
    logger.info("  Windows  : %s (local time)", ", ".join(times))
    logger.info("  Run log  : %s", RUN_LOG_FILE)
    logger.info("  Health   : concurrent-run guard + token pre-validation enabled")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("Press Ctrl+C to stop.\n")

    for t in times:
        label = t
        schedule.every().day.at(t).do(
            _run_window,
            window_label=label,
            count=count,
            image=use_image,
            dry_run=dry_run,
        )
        logger.info("  ⏰ Scheduled: %s", t)

    # Run first batch immediately on start
    logger.info("\n🚀 Running first batch now (window: startup)...")
    _run_window(
        window_label="startup",
        count=count,
        image=use_image,
        dry_run=dry_run,
    )
    logger.info("\n✅ First batch done. Waiting for next scheduled window...")

    try:
        while True:
            schedule.run_pending()
            next_run = schedule.next_run()
            if next_run:
                delta      = next_run - datetime.now()
                total_mins = int(delta.total_seconds() / 60)
                if total_mins % 30 == 0 and int(delta.total_seconds()) % 60 < 2:
                    active_note = " (⚠️  run still active)" if _is_run_active() else ""
                    logger.info(
                        "⏳ Next window in %dh %dm (%s)%s",
                        total_mins // 60, total_mins % 60,
                        next_run.strftime("%H:%M"),
                        active_note,
                    )
            time.sleep(60)
    except KeyboardInterrupt:
        if _is_run_active():
            logger.info("🛑 Scheduler stopped — waiting for active run to finish...")
            with _active_process_lock:
                proc = _active_process
            if proc:
                proc.wait()
        logger.info("🛑 Scheduler stopped by user.")


if __name__ == "__main__":
    main()
