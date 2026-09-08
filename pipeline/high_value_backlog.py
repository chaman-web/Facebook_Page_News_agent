"""Persistent retry safety net for unpublished high-value stories."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import config
from models import Story


def remember(story: Story, score: float, impact_score: float) -> None:
    data = _load()
    story.priority_protected = True
    data[story.source_url] = {
        "title": story.title,
        "source_name": story.source_name,
        "source_url": story.source_url,
        "published_at": story.published_at.isoformat(),
        "raw_summary": story.raw_summary,
        "category": story.category,
        "region": story.region,
        "score": score,
        "impact_score": impact_score,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(data)


def pending_stories(max_age_hours: int | None = None) -> list[Story]:
    max_age_hours = max_age_hours or config.NEWS_MAX_AGE_HOURS
    now = datetime.now(timezone.utc)
    data = _load()
    active: dict = {}
    stories: list[Story] = []
    for url, item in data.items():
        try:
            published_at = datetime.fromisoformat(item["published_at"])
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            if (now - published_at).total_seconds() > max_age_hours * 3600:
                continue
            stories.append(Story(
                title=item["title"],
                source_name=item["source_name"],
                source_url=url,
                published_at=published_at,
                raw_summary=item.get("raw_summary", ""),
                category=item.get("category", "world"),
                region=item.get("region", "global"),
                priority_protected=True,
            ))
            active[url] = item
        except (KeyError, TypeError, ValueError):
            continue
    if active != data:
        _save(active)
    return stories


def resolve(source_url: str) -> None:
    data = _load()
    if source_url in data:
        del data[source_url]
        _save(data)


def _load() -> dict:
    path = Path(config.HIGH_VALUE_BACKLOG_PATH)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    path = Path(config.HIGH_VALUE_BACKLOG_PATH)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
