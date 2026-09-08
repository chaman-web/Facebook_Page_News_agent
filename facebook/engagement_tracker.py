"""Collect lightweight engagement snapshots for published Facebook posts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

import config
from facebook.publisher import GRAPH_API_URL
from facebook.token_manager import TokenManager

logger = logging.getLogger(__name__)


def _summary_count(value: dict | None) -> int:
    return int(((value or {}).get("summary") or {}).get("total_count", 0) or 0)


def fetch_post_metrics(post_id: str, token: str) -> dict:
    response = requests.get(
        f"{GRAPH_API_URL}/{post_id}",
        params={
            "fields": "created_time,shares,reactions.limit(0).summary(true),comments.limit(0).summary(true)",
            "access_token": token,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return {
        "facebook_post_id": post_id,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "created_time": data.get("created_time"),
        "reactions": _summary_count(data.get("reactions")),
        "comments": _summary_count(data.get("comments")),
        "shares": int((data.get("shares") or {}).get("count", 0) or 0),
    }


def collect_published_metrics(entries: list, output_path: Path | None = None) -> list[dict]:
    output_path = output_path or config.ENGAGEMENT_METRICS_PATH
    eligible = [
        entry for entry in entries
        if entry.status == "PUBLISHED" and getattr(entry, "facebook_post_id", None)
    ]
    if not eligible:
        logger.info("No published posts with Facebook IDs are available for engagement tracking.")
        return []

    token = TokenManager().get_valid_token()
    snapshots: list[dict] = []
    for entry in eligible:
        try:
            snapshot = fetch_post_metrics(entry.facebook_post_id, token)
        except requests.RequestException as exc:
            logger.warning("Could not collect engagement for %s: %s", entry.facebook_post_id, exc)
            continue
        snapshot.update({"title": entry.title, "category": entry.category})
        snapshots.append(snapshot)

    if snapshots:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as handle:
            for snapshot in snapshots:
                handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return snapshots
