#!/usr/bin/env python3
"""
audit.py — Daily summary of posting decisions for Global Pulse News.

Reads posting_decisions.jsonl and prints a human-readable report showing:
  - What published today (and why)
  - What was held (and why)
  - What was rejected (and why)
  - Publish rate by lane
  - Top rejection reasons
  - Score distribution

Usage:
    python audit.py              # today's decisions
    python audit.py --days 3     # last 3 days
    python audit.py --all        # full history
    python audit.py --lane HOLD  # filter by lane
    python audit.py --csv        # export as CSV
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

AUDIT_FILE = Path("posting_decisions.jsonl")

LANE_ICONS = {
    "PUBLISH_NOW": "🚨",
    "NEXT_SLOT":   "⚡",
    "SCHEDULE":    "📅",
    "HOLD":        "🗂️ ",
    "REJECT":      "🗑️ ",
}

DECISION_ICONS = {
    "PUBLISHED":  "✅",
    "QUEUED":     "📥",
    "REJECT":     "🗑️ ",
    "HOLD":       "🗂️ ",
    "SKIPPED":    "⏩",
    "EXPIRED":    "⏰",
    "DOWNGRADED": "⬇️ ",
}


def load_entries(days: int | None = 1, all_history: bool = False) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    entries = []
    cutoff  = None if all_history else (
        datetime.now(timezone.utc) - timedelta(days=days or 1)
    )
    with AUDIT_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["ts"])
                if cutoff is None or ts >= cutoff:
                    entries.append(e)
            except (json.JSONDecodeError, KeyError):
                continue
    return entries


def _bar(count: int, total: int, width: int = 20) -> str:
    filled = int(width * count / total) if total > 0 else 0
    return "█" * filled + "░" * (width - filled)


def print_summary(entries: list[dict], lane_filter: str | None = None) -> None:
    if not entries:
        print("  No entries found.")
        return

    if lane_filter:
        entries = [e for e in entries if e.get("lane") == lane_filter]
        if not entries:
            print(f"  No entries for lane={lane_filter}.")
            return

    # Group by date
    by_date: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        ts   = datetime.fromisoformat(e["ts"])
        day  = ts.date().isoformat()
        by_date[day].append(e)

    for day in sorted(by_date.keys(), reverse=True):
        day_entries = by_date[day]
        _print_day(day, day_entries)


def _print_day(day: str, entries: list[dict]) -> None:
    total      = len(entries)
    published  = [e for e in entries if e["decision"] == "PUBLISHED"]
    rejected   = [e for e in entries if e["decision"] in ("REJECT", "EXPIRED")]
    held       = [e for e in entries if e["decision"] in ("HOLD", "SKIPPED")]
    downgraded = [e for e in entries if e["decision"] == "DOWNGRADED"]
    queued     = [e for e in entries if e["decision"] == "QUEUED"]

    pub_rate = len(published) / max(len(queued) + len(published), 1) * 100

    print()
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  📅 {day}  ({total} decisions)")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  ✅ Published  : {len(published):3d}   ({pub_rate:.0f}% of queued+published)")
    print(f"  🗑️  Rejected   : {len(rejected):3d}")
    print(f"  ⏩ Held/Skip  : {len(held):3d}")
    print(f"  ⬇️  Downgraded : {len(downgraded):3d}")
    print(f"  📥 Queued     : {len(queued):3d}")

    # Score distribution
    scores = [e["score"] for e in entries if "score" in e]
    if scores:
        avg  = sum(scores) / len(scores)
        hi   = max(scores)
        lo   = min(scores)
        print(f"\n  Score range  : {lo:.0f}–{hi:.0f}  (avg {avg:.1f})")

    # Lane breakdown
    lane_counts = Counter(e.get("lane", "?") for e in entries)
    if lane_counts:
        print(f"\n  Lane breakdown:")
        total_laned = sum(lane_counts.values())
        for lane, count in sorted(lane_counts.items(), key=lambda x: -x[1]):
            icon = LANE_ICONS.get(lane, "  ")
            bar  = _bar(count, total_laned)
            print(f"    {icon} {lane:<14} {count:3d}  {bar}")

    # Published stories
    if published:
        print(f"\n  ✅ Published ({len(published)}):")
        for e in sorted(published, key=lambda x: x["ts"]):
            ts_str = datetime.fromisoformat(e["ts"]).strftime("%H:%M")
            icon   = LANE_ICONS.get(e.get("lane", ""), "  ")
            print(f"    {ts_str}  {icon} [{e['score']:5.1f} T{e['tier']}]  {e['title'][:60]}")

    # Top rejection reasons
    if rejected:
        reasons = Counter(e.get("reason", "unknown")[:60] for e in rejected)
        print(f"\n  🗑️  Top rejection reasons:")
        for reason, count in reasons.most_common(5):
            print(f"    ×{count:2d}  {reason}")

    # Top hold reasons
    if held:
        reasons = Counter(e.get("reason", "unknown")[:60] for e in held)
        print(f"\n  ⏩ Top hold reasons:")
        for reason, count in reasons.most_common(5):
            print(f"    ×{count:2d}  {reason}")

    # Downgraded (TTL)
    if downgraded:
        print(f"\n  ⬇️  TTL downgrades ({len(downgraded)}):")
        for e in downgraded[:5]:
            print(f"    [{e['score']:5.1f}] {e['title'][:55]}  ({e['reason'][:50]})")
        if len(downgraded) > 5:
            print(f"    … and {len(downgraded) - 5} more")


def export_csv(entries: list[dict]) -> None:
    out = io.StringIO()
    fieldnames = ["ts", "title", "score", "tier", "lane", "decision", "reason"]
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for e in entries:
        writer.writerow(e)
    print(out.getvalue())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Global Pulse News — Posting Audit Report"
    )
    parser.add_argument("--days",  type=int, default=1,
                        help="Number of past days to show (default: 1 = today)")
    parser.add_argument("--all",   action="store_true",
                        help="Show full history")
    parser.add_argument("--lane",  choices=["PUBLISH_NOW", "NEXT_SLOT", "SCHEDULE", "HOLD", "REJECT"],
                        help="Filter by lane")
    parser.add_argument("--csv",   action="store_true",
                        help="Export as CSV to stdout")
    args = parser.parse_args()

    if not AUDIT_FILE.exists():
        print(f"\n  No audit log found at {AUDIT_FILE}.")
        print("  The log is created automatically when stories are processed.")
        sys.exit(0)

    entries = load_entries(days=args.days, all_history=args.all)

    if args.csv:
        export_csv(entries)
        return

    period = "full history" if args.all else (
        f"today" if args.days == 1 else f"last {args.days} days"
    )
    lane_note = f"  (lane={args.lane})" if args.lane else ""
    print(f"\n  Global Pulse News — Posting Audit  [{period}]{lane_note}")

    if not entries:
        print(f"\n  No entries in the last {args.days} day(s).")
        sys.exit(0)

    print_summary(entries, lane_filter=args.lane)
    print()


if __name__ == "__main__":
    main()
