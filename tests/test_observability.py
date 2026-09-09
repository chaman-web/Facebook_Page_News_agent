import json
import logging
from datetime import datetime, timezone

from pipeline.observability import RunIdFilter, write_daily_summary


def test_run_id_filter_adds_identifier():
    record = logging.LogRecord("test", logging.INFO, "", 0, "message", (), None)
    assert RunIdFilter("run-123").filter(record)
    assert record.run_id == "run-123"


def test_daily_summary_keeps_only_two_dates(tmp_path):
    (tmp_path / "daily_summary_2026-09-07.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "daily_summary_2026-09-08.jsonl").write_text("{}\n", encoding="utf-8")

    path = write_daily_summary(
        "fetch",
        "completed",
        {"fetched": 20, "high_impact_missed": 0},
        run_id="run-123",
        now=datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc),
        directory=tmp_path,
    )

    assert not (tmp_path / "daily_summary_2026-09-07.jsonl").exists()
    assert (tmp_path / "daily_summary_2026-09-08.jsonl").exists()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["run_id"] == "run-123"
    assert record["high_impact_missed"] == 0
