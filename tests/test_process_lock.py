import threading
import time

from pipeline.process_lock import pipeline_lock


def test_waiting_job_acquires_lock_after_short_overlap(tmp_path):
    lock_path = tmp_path / ".pipeline.lock"
    acquired = threading.Event()

    def waiter():
        with pipeline_lock(lock_path, timeout_seconds=1):
            acquired.set()

    with pipeline_lock(lock_path):
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        assert not acquired.is_set()

    thread.join(timeout=2)
    assert acquired.is_set()
