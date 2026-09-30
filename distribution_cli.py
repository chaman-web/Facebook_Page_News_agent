"""Standalone preview for manual Facebook Group recommendations; never posts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from distribution.models import DistributionStory
from distribution.recommender import DistributionRecommender

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview manual-only Group recommendations.")
    parser.add_argument("--story", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=ROOT / "distribution" / "groups.json")
    parser.add_argument("--ledger", type=Path, default=ROOT / "distribution_ledger.jsonl")
    parser.add_argument("--preview", action="store_true", required=True, help="Required in Phase 1; performs no sharing and writes no ledger.")
    args = parser.parse_args()
    story = DistributionStory(**json.loads(args.story.read_text(encoding="utf-8")))
    results = DistributionRecommender(args.registry, args.ledger).recommend(story, preview=True)
    print(json.dumps({"group_auto_share_enabled": False, "group_post_performed": False, "results": [item.to_dict() for item in results]}, indent=2))
    return 0


if __name__ == "__main__": raise SystemExit(main())
