from datetime import date

from pipeline.log_retention import prune_log_file, prune_rotated_logs, retained_log_handler


def test_log_file_keeps_only_today_and_yesterday(tmp_path):
    path = tmp_path / "job.log"
    path.write_text(
        "2026-09-07 23:59:00  INFO      old entry\n"
        "old continuation\n"
        "2026-09-08 00:00:00  WARNING   yesterday entry\n"
        "yesterday continuation\n"
        "2026-09-09 08:00:00  INFO      today entry\n",
        encoding="utf-8",
    )

    removed = prune_log_file(path, today=date(2026, 9, 9))

    content = path.read_text(encoding="utf-8")
    assert removed == 2
    assert "2026-09-07" not in content
    assert "old continuation" not in content
    assert "2026-09-08" in content
    assert "yesterday continuation" in content
    assert "2026-09-09" in content


def test_old_rotated_logs_are_removed(tmp_path):
    path = tmp_path / "job.log"
    path.touch()
    (tmp_path / "job.log.2026-09-07").touch()
    (tmp_path / "job.log.2026-09-08").touch()

    removed = prune_rotated_logs(path, today=date(2026, 9, 9))

    assert removed == 1
    assert not (tmp_path / "job.log.2026-09-07").exists()
    assert (tmp_path / "job.log.2026-09-08").exists()


def test_handler_rotates_daily_with_one_backup(tmp_path):
    handler = retained_log_handler(tmp_path / "job.log")
    try:
        assert handler.when == "MIDNIGHT"
        assert handler.backupCount == 1
        assert handler.encoding.lower().replace("-", "") == "utf8"
    finally:
        handler.close()
