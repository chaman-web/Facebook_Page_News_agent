"""Show high-value stories that are waiting, blocked, or failed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-score", type=float, default=70)
    args = parser.parse_args()

    queue_data = _read_json(config.POSTING_QUEUE_PATH, {"entries": []})
    entries = queue_data.get("entries", []) if isinstance(queue_data, dict) else queue_data
    active = [
        item for item in entries
        if item.get("status") in {"QUEUED", "REVIEW_REQUIRED"}
        and float(item.get("score", 0)) >= args.min_score
    ]
    backlog = _read_json(config.HIGH_VALUE_BACKLOG_PATH, {})
    latest_drafts = {}
    for path in sorted(config.DRAFTS_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime):
        draft = _read_json(path, {})
        if draft.get("title"):
            latest_drafts[draft["title"]] = draft

    print(f"HIGH-VALUE AUDIT (minimum score {args.min_score:g})")
    print("\nBACKLOG — protected, awaiting another processing attempt")
    if backlog:
        for item in sorted(backlog.values(), key=lambda row: float(row.get("score", 0)), reverse=True):
            print(f"  {float(item.get('score', 0)):5.1f} | {item.get('title', '')}")
            draft = latest_drafts.get(item.get("title"), {})
            reason = draft.get("verification_reason") or draft.get("rejection_reason")
            if reason:
                print(f"          Reason: {reason}")
    else:
        print("  None")

    print("\nACTIVE QUEUE — not yet published")
    if active:
        for item in sorted(active, key=lambda row: float(row.get("score", 0)), reverse=True):
            print(
                f"  {float(item.get('score', 0)):5.1f} | {item.get('status', ''):15} | "
                f"{item.get('route', ''):12} | {item.get('title', '')}"
            )
            if item.get("status") == "REVIEW_REQUIRED" and item.get("verification_reason"):
                print(f"          Reason: {item['verification_reason']}")
    else:
        print("  None")

    print("\nLATEST ERRORS — fetch/build and publish logs")
    found = False
    for filename in ("job1_fetch.log", "job2_publish.log"):
        path = config.LOGS_DIR / filename
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        errors = [line for line in lines if " ERROR " in line or " failed" in line.lower()]
        for line in errors[-10:]:
            found = True
            print(f"  [{filename}] {line}")
    if not found:
        print("  None")


if __name__ == "__main__":
    main()
