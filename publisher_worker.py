"""Queue-aware launcher for the on-demand Facebook publisher task."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys

import config
from models import VerificationStatus
from pipeline.posting_queue import PostingQueue

logger = logging.getLogger(__name__)


def has_publishable_work(queue: PostingQueue | None = None) -> bool:
    """Return whether either tier contains a verified queued story."""
    current = queue or PostingQueue()
    return any(
        entry.status == "QUEUED"
        and entry.verification_status == VerificationStatus.VERIFIED.value
        for entry in current._entries
    )


def run_worker(*, run=subprocess.run) -> int:
    """Run one publisher cycle; the task trigger handles retries while enabled."""
    queue = PostingQueue()
    if not has_publishable_work(queue):
        logger.info("Publisher worker idle: no verified Tier 1 or Tier 2 stories queued.")
        return 0

    result = run(
        [sys.executable, str(config.PROJECT_ROOT / "agent.py"), "--publish", "--count", "1"],
        cwd=config.PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        logger.error("Publisher cycle failed with exit code %d.", result.returncode)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue-aware Facebook publisher worker")
    parser.add_argument(
        "--has-work",
        action="store_true",
        help="Exit 0 when verified queued work exists; otherwise exit 1.",
    )
    args = parser.parse_args()
    if args.has_work:
        return 0 if has_publishable_work() else 1
    return run_worker()


if __name__ == "__main__":
    raise SystemExit(main())
