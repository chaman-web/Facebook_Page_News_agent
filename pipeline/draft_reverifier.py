"""
pipeline/draft_reverifier.py — Re-verify single-source DRAFT stories on each fetch run.

How it works
------------
On every fetch run, before new stories are fetched, this module scans the
drafts/ folder for stories that were saved as DRAFT (single-source, unverified).
Each DRAFT is re-submitted to verify_story() against the current RSS pool, which
by then may contain corroborating reports from a second independent source.

If a DRAFT is now confirmed by 2+ independent sources it is promoted:
  - verification_status → VERIFIED
  - draft_status        → READY_FOR_REVIEW
  - Returned to the caller so agent.py can rebuild the post and queue it

If still single-source: attempt counter is incremented, draft file is updated.
After MAX_REVERIFY_ATTEMPTS the story is left as DRAFT and never checked again
(prevents stale single-source stories looping forever).

Rules
-----
- Only DRAFT stories (not REJECTED, not POLICY_REVIEW) are re-checked
- Only stories younger than 48 hours (stale news has no value)
- Max 3 re-verification attempts per story
- Never modifies the existing pipeline stages or queue logic
- Always exits cleanly — a reverify failure never blocks the fetch run
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
from models import Story, StoryRejected, VerificationStatus, DraftStatus
from pipeline.verifier import verify_story

logger = logging.getLogger(__name__)

MAX_REVERIFY_ATTEMPTS = 3
MAX_AGE_HOURS = 48  # don't re-check drafts older than this
_REVERIFY_KEY = "reverify_attempts"  # extra field stored in draft JSON


def reverify_drafts() -> list[Story]:
    """
    Scan drafts/ for single-source DRAFT stories and re-verify them.

    Returns a list of Story objects that were promoted to VERIFIED.
    These should be passed back into the normal pipeline (rebuild post → queue).

    Always returns an empty list on any error — never raises.
    """
    try:
        return _run()
    except Exception as exc:
        logger.warning("Draft re-verifier encountered an error — skipping: %s", exc)
        return []


def _run() -> list[Story]:
    drafts_dir = Path(config.DRAFTS_DIR)
    if not drafts_dir.exists():
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    promoted: list[Story] = []
    checked = 0
    still_single = 0

    for json_path in sorted(drafts_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        # Only re-check DRAFT status (not REJECTED, POLICY_REVIEW, READY_FOR_REVIEW)
        if data.get("draft_status") != DraftStatus.DRAFT.value:
            continue

        # Only re-check UNVERIFIED (not SPECULATIVE)
        if data.get("verification_status") not in (
            VerificationStatus.UNVERIFIED.value, "UNVERIFIED",
        ):
            continue

        # Skip if too many attempts already
        attempts = int(data.get(_REVERIFY_KEY, 0))
        if attempts >= MAX_REVERIFY_ATTEMPTS:
            continue

        # Skip if too old
        try:
            published_at = datetime.fromisoformat(data["published_at"])
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            if published_at < cutoff:
                continue
        except (KeyError, ValueError):
            continue

        # Rebuild a minimal Story object for re-verification
        try:
            story = _story_from_draft(data)
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Could not rebuild story from draft %s: %s", json_path.name, exc)
            continue

        checked += 1

        # Re-run verification against current RSS pool
        try:
            story = verify_story(story, deep=False)
        except StoryRejected:
            # Still rejected — increment attempt counter and move on
            _increment_attempts(json_path, data, attempts)
            still_single += 1
            continue
        except Exception as exc:
            logger.debug("verify_story raised unexpectedly for '%s': %s", story.title[:50], exc)
            _increment_attempts(json_path, data, attempts)
            continue

        if story.verification_status == VerificationStatus.VERIFIED:
            # Promoted — update draft file to reflect new status
            data["verification_status"] = VerificationStatus.VERIFIED.value
            data["verification_score"] = story.verification_score
            data["verification_reason"] = story.verification_reason
            data["draft_status"] = DraftStatus.READY_FOR_REVIEW.value
            data[_REVERIFY_KEY] = attempts + 1
            try:
                json_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except OSError as exc:
                logger.warning("Could not update draft file %s: %s", json_path.name, exc)

            logger.info(
                "📬 Draft promoted to VERIFIED after re-check [score %.0f]: %s",
                story.verification_score,
                story.title[:70],
            )
            promoted.append(story)
        else:
            # Still single-source
            _increment_attempts(json_path, data, attempts)
            still_single += 1

    if checked:
        logger.info(
            "Draft re-verifier: checked %d DRAFT(s) — %d promoted, %d still single-source.",
            checked, len(promoted), still_single,
        )

    return promoted


def _story_from_draft(data: dict) -> Story:
    """Reconstruct a minimal Story from a draft JSON dict."""
    published_at = datetime.fromisoformat(data["published_at"])
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    story = Story(
        title=data["title"],
        source_name=data["source_name"],
        source_url=data["source_url"],
        published_at=published_at,
        raw_summary=data.get("news_summary") or data.get("raw_summary") or "",
        category=data.get("category", "world"),
        region=data.get("region", "global"),
    )
    # Carry forward corroborating sources from the original draft so the
    # verifier can re-use previously found evidence
    story.corroborating_sources = data.get("corroborating_sources") or []
    story.article_text = data.get("article_text") or ""
    story.verification_status = VerificationStatus.UNVERIFIED
    story.verification_score = float(data.get("verification_score") or 0.0)
    return story


def _increment_attempts(path: Path, data: dict, current: int) -> None:
    """Write incremented attempt counter back to the draft file."""
    data[_REVERIFY_KEY] = current + 1
    try:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
