r"""Merge runtime state accidentally written outside the project directory.

Windows Task Scheduler starts tasks in ``C:\Windows\System32`` unless a
working directory is configured. Older task definitions therefore created a
second queue and history there. This migration is intentionally conservative:
it backs up both sides, keeps terminal queue states over queued copies, and
deduplicates only exact URLs or near-identical titles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import config


TERMINAL_RANK = {"PUBLISHED": 4, "SKIPPED": 3, "EXPIRED": 3, "QUEUED": 2}


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", title.lower()).strip()


def _same_story(left: dict, right: dict) -> bool:
    if left.get("source_url") and left.get("source_url") == right.get("source_url"):
        return True
    a = _normalise_title(left.get("title", ""))
    b = _normalise_title(right.get("title", ""))
    return bool(a and b and SequenceMatcher(None, a, b).ratio() >= 0.92)


def _entry_rank(entry: dict) -> tuple[int, int, str]:
    completeness = sum(bool(entry.get(k)) for k in ("post_content", "image_path", "card_headline"))
    return (
        TERMINAL_RANK.get(entry.get("status", ""), 0),
        completeness,
        entry.get("published_at") or entry.get("queued_at") or "",
    )


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_entry_image(entry: dict, legacy_root: Path) -> bool:
    raw_path = entry.get("image_path")
    if not raw_path:
        return False
    source = Path(raw_path)
    if not source.is_absolute():
        source = legacy_root / source
    if not source.exists():
        entry["image_path"] = None
        return False

    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    target = config.IMAGES_DIR / source.name
    if target.exists() and _hash(source) != _hash(target):
        target = config.IMAGES_DIR / f"legacy-{source.name}"
    if not target.exists():
        shutil.copy2(source, target)
    entry["image_path"] = str(target.resolve())
    return True


def _normalise_project_image(entry: dict) -> None:
    raw_path = entry.get("image_path")
    if not raw_path:
        return
    image_path = Path(raw_path)
    if not image_path.is_absolute():
        image_path = config.PROJECT_ROOT / image_path
    entry["image_path"] = str(image_path.resolve()) if image_path.exists() else None


def _merge_queue(project: dict, legacy: dict, legacy_root: Path) -> tuple[dict, int, int]:
    merged = [dict(entry) for entry in project.get("entries", [])]
    for entry in merged:
        _normalise_project_image(entry)

    duplicates = 0
    images_copied = 0
    for original in legacy.get("entries", []):
        candidate = dict(original)
        match_index = next((i for i, item in enumerate(merged) if _same_story(item, candidate)), None)
        if match_index is None:
            images_copied += int(_copy_entry_image(candidate, legacy_root))
            merged.append(candidate)
            continue
        duplicates += 1
        if _entry_rank(candidate) > _entry_rank(merged[match_index]):
            images_copied += int(_copy_entry_image(candidate, legacy_root))
            merged[match_index] = candidate

    merged.sort(key=lambda item: item.get("queued_at") or "")
    return {"entries": merged}, duplicates, images_copied


def _merge_seen(project: dict, legacy: dict) -> dict:
    result = dict(project)
    for key in ("urls", "titles"):
        result[key] = list(dict.fromkeys([*project.get(key, []), *legacy.get(key, [])]))
    for key in ("url_attempts", "title_attempts"):
        combined = dict(project.get(key, {}))
        for item, candidate in legacy.get(key, {}).items():
            current = combined.get(item)
            if not current:
                combined[item] = candidate
                continue
            combined[item] = {
                "count": max(int(current.get("count", 0)), int(candidate.get("count", 0))),
                "first_seen": min(current.get("first_seen", "9999"), candidate.get("first_seen", "9999")),
                "last_seen": max(current.get("last_seen", ""), candidate.get("last_seen", "")),
            }
        result[key] = combined
    return result


def _merge_jsonl(project_path: Path, legacy_path: Path) -> int:
    existing = project_path.read_text(encoding="utf-8").splitlines() if project_path.exists() else []
    incoming = legacy_path.read_text(encoding="utf-8").splitlines() if legacy_path.exists() else []
    unique = list(dict.fromkeys(line for line in [*existing, *incoming] if line.strip()))

    def timestamp(line: str) -> str:
        try:
            record = json.loads(line)
            return record.get("ts") or record.get("timestamp") or ""
        except json.JSONDecodeError:
            return ""

    unique.sort(key=timestamp)
    project_path.write_text("\n".join(unique) + ("\n" if unique else ""), encoding="utf-8")
    return len(unique) - len(existing)


def _backup(project_paths: list[Path], legacy_paths: list[Path], legacy_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = config.PROJECT_ROOT / ".runtime-backups" / stamp
    for label, paths in (("project", project_paths), ("legacy-system32", legacy_paths)):
        target_dir = root / label
        target_dir.mkdir(parents=True, exist_ok=True)
        for path in paths:
            if path.exists():
                shutil.copy2(path, target_dir / path.name)
    legacy_images = legacy_root / "images"
    if legacy_images.exists():
        shutil.copytree(legacy_images, root / "legacy-system32" / "images")
    return root


def reconcile(legacy_root: Path) -> dict:
    file_names = (
        "posting_queue.json",
        "seen_stories.json",
        "posting_decisions.jsonl",
        "score_log.jsonl",
        "source_health.json",
    )
    project_paths = [config.PROJECT_ROOT / name for name in file_names]
    legacy_paths = [legacy_root / name for name in file_names]
    backup_root = _backup(project_paths, legacy_paths, legacy_root)

    project_queue = _load_json(config.POSTING_QUEUE_PATH, {"entries": []})
    legacy_queue = _load_json(legacy_root / "posting_queue.json", {"entries": []})
    merged_queue, duplicates, images_copied = _merge_queue(project_queue, legacy_queue, legacy_root)
    config.POSTING_QUEUE_PATH.write_text(
        json.dumps(merged_queue, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    project_seen = _load_json(config.SEEN_STORIES_PATH, {})
    legacy_seen = _load_json(legacy_root / "seen_stories.json", {})
    if project_seen or legacy_seen:
        config.SEEN_STORIES_PATH.write_text(
            json.dumps(_merge_seen(project_seen, legacy_seen), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    decisions_added = _merge_jsonl(
        config.POSTING_DECISIONS_PATH, legacy_root / "posting_decisions.jsonl"
    )
    scores_added = _merge_jsonl(config.SCORE_LOG_PATH, legacy_root / "score_log.jsonl")

    return {
        "backup": str(backup_root),
        "project_entries_before": len(project_queue.get("entries", [])),
        "legacy_entries": len(legacy_queue.get("entries", [])),
        "duplicates_removed": duplicates,
        "merged_entries": len(merged_queue["entries"]),
        "queue_images_copied": images_copied,
        "decision_records_added": decisions_added,
        "score_records_added": scores_added,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-root",
        type=Path,
        default=Path(r"C:\Windows\System32"),
        help="Directory containing the accidentally-created runtime files.",
    )
    args = parser.parse_args()
    print(json.dumps(reconcile(args.legacy_root.resolve()), indent=2))


if __name__ == "__main__":
    main()
