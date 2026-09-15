"""
tests/test_schedule_monitor.py — Tests for pipeline/schedule_monitor.py

Verifies:
- On-time jobs produce no warning
- Late jobs log a warning and write a late_triggers.jsonl record
- Jobs with no fixed windows (publish) are skipped silently
- Always exits 0 — never blocks the pipeline
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.schedule_monitor import (
    LATE_THRESHOLD_MINUTES,
    check_lateness,
    nearest_window,
)


# ---------------------------------------------------------------------------
# nearest_window tests
# ---------------------------------------------------------------------------

class TestNearestWindow:
    def test_returns_most_recent_past_window(self):
        windows = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]
        now = datetime(2026, 9, 12, 12, 45, 0)
        expected, label = nearest_window(now, windows)
        assert label == "12:00"
        assert expected.hour == 12
        assert expected.minute == 0

    def test_returns_midnight_window_just_after_midnight(self):
        windows = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]
        now = datetime(2026, 9, 12, 0, 5, 0)
        expected, label = nearest_window(now, windows)
        assert label == "00:00"

    def test_exact_window_time_has_zero_delta(self):
        windows = ["08:00", "12:00", "16:00"]
        now = datetime(2026, 9, 12, 8, 0, 0)
        expected, label = nearest_window(now, windows)
        assert label == "08:00"
        delta = (now - expected).total_seconds() / 60
        assert delta == 0.0


# ---------------------------------------------------------------------------
# check_lateness tests
# ---------------------------------------------------------------------------

class TestCheckLateness:
    def test_on_time_job_writes_no_warning(self, tmp_path):
        """Job running within threshold — no warning file entry."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            # 08:05 — 5 min after 08:00, within 15-min threshold
            check_lateness("fetch", _now=datetime(2026, 9, 12, 8, 5, 0))

        late_log = tmp_path / "late_triggers.jsonl"
        assert not late_log.exists(), "Should not write late_triggers.jsonl for on-time job"

    def test_late_job_writes_warning_log(self, tmp_path):
        """Late job logs a WARNING level message."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            with patch("pipeline.schedule_monitor.WARNING_LOG", tmp_path / "schedule_warnings.log"):
                # Re-add file handler pointing to tmp_path
                import pipeline.schedule_monitor as sm
                handler = logging.FileHandler(tmp_path / "schedule_warnings.log", encoding="utf-8")
                handler.setLevel(logging.WARNING)
                sm.logger.addHandler(handler)
                try:
                    check_lateness("fetch", _now=datetime(2026, 9, 12, 12, 56, 0))
                finally:
                    sm.logger.removeHandler(handler)
                    handler.close()

        warning_log = tmp_path / "schedule_warnings.log"
        assert warning_log.exists(), "Warning log should be created for late job"
        content = warning_log.read_text()
        assert "fetch" in content
        assert "late" in content.lower()

    def test_late_job_writes_jsonl_record(self, tmp_path):
        """Late job writes a structured JSON record to late_triggers.jsonl."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            check_lateness("fetch", _now=datetime(2026, 9, 12, 12, 56, 0))

        late_log = tmp_path / "late_triggers.jsonl"
        assert late_log.exists()
        record = json.loads(late_log.read_text().strip())
        assert record["job"] == "fetch"
        assert record["expected_window"] == "12:00"
        assert record["actual_time"] == "12:56"
        assert record["delay_minutes"] == pytest.approx(56.0, abs=1.0)

    def test_publish_job_has_no_fixed_windows_skipped_silently(self, tmp_path):
        """Publish job has no fixed windows — no late files written."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            check_lateness("publish", _now=datetime(2026, 9, 12, 12, 56, 0))

        late_log = tmp_path / "late_triggers.jsonl"
        assert not late_log.exists(), "Publish job should not write late_triggers.jsonl"

    def test_exactly_at_threshold_is_not_late(self, tmp_path):
        """Job at exactly LATE_THRESHOLD_MINUTES is not flagged."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            check_lateness(
                "fetch",
                _now=datetime(2026, 9, 12, 8, 0 + LATE_THRESHOLD_MINUTES, 0),
            )

        late_log = tmp_path / "late_triggers.jsonl"
        assert not late_log.exists(), "Exactly at threshold should not be flagged"

    def test_one_minute_past_threshold_is_late(self, tmp_path):
        """Job one minute past threshold is flagged."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            check_lateness(
                "fetch",
                _now=datetime(2026, 9, 12, 8, 0 + LATE_THRESHOLD_MINUTES + 1, 0),
            )

        late_log = tmp_path / "late_triggers.jsonl"
        assert late_log.exists(), "One minute past threshold should be flagged"

    def test_multiple_late_runs_append_to_jsonl(self, tmp_path):
        """Multiple late runs append separate records to the same file."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            check_lateness("fetch", _now=datetime(2026, 9, 12, 4, 45, 0))
            check_lateness("fetch", _now=datetime(2026, 9, 12, 8, 45, 0))

        late_log = tmp_path / "late_triggers.jsonl"
        lines = late_log.read_text().strip().splitlines()
        assert len(lines) == 2, "Should have two separate late records"

    def test_check_lateness_always_returns_none_never_raises(self, tmp_path):
        """check_lateness never raises and always returns None."""
        with patch("pipeline.schedule_monitor.LOGS_DIR", tmp_path):
            result = check_lateness("fetch", _now=datetime(2026, 9, 12, 13, 30, 0))
        assert result is None
