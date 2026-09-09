"""Small cross-process lock for scheduled fetch and publish jobs."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class PipelineBusy(RuntimeError):
    pass


@contextmanager
def pipeline_lock(path: Path, *, timeout_seconds: float = 0, stale_seconds: int = 5400):
    deadline = time.monotonic() + timeout_seconds
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "acquired_at": datetime.now(timezone.utc).isoformat()}, handle)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_seconds:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise PipelineBusy("another fetch or publish process is already active")
            time.sleep(0.25)
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
