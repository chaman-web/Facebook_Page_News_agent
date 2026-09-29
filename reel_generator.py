"""Explicit CLI for offline Reel generation. This command never publishes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from reels.generator import ReelGenerationError, ReelGenerator, ReelInput


PROJECT_ROOT = Path(__file__).resolve().parent


def _load_queue_entry(queue_path: Path, index: int) -> ReelInput:
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    if not entries:
        raise ReelGenerationError("the queue contains no entries")
    try:
        entry = entries[index]
    except IndexError as exc:
        raise ReelGenerationError(f"queue index {index} does not exist") from exc
    return ReelInput(
        title=entry.get("title", ""),
        source_name=entry.get("source_name", ""),
        source_url=entry.get("source_url", ""),
        category=entry.get("category", "news"),
        post_content=entry.get("post_content") or "",
        card_headline=entry.get("card_headline") or "",
        card_description=entry.get("card_description") or "",
        image_path=entry.get("image_path"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local vertical news Reel; never publish it.")
    parser.add_argument("--queue", type=Path, default=PROJECT_ROOT / "posting_queue.json")
    parser.add_argument("--index", type=int, default=-1, help="Queue entry index (default: latest)")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reels_output")
    parser.add_argument("--frames-only", action="store_true", help="Skip MP4 assembly")
    args = parser.parse_args()

    try:
        item = _load_queue_entry(args.queue, args.index)
        result = ReelGenerator(args.output).generate(item, render_video=not args.frames_only)
    except (OSError, ValueError, json.JSONDecodeError, ReelGenerationError) as exc:
        parser.error(str(exc))
    print(json.dumps(result.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
