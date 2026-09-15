"""
tests/test_draft_reverifier.py — Tests for pipeline/draft_reverifier.py

Scenarios:
  1.  Empty drafts folder → returns []
  2.  READY_FOR_REVIEW draft → skipped (not a DRAFT)
  3.  REJECTED draft → skipped
  4.  DRAFT older than 48 h → skipped
  5.  DRAFT with max attempts reached → skipped
  6.  DRAFT that verifies on re-check → promoted, returned in list
  7.  DRAFT still single-source → attempt counter incremented, not promoted
  8.  DRAFT that raises StoryRejected → attempt counter incremented
  9.  Multiple DRAFTs — only eligible ones checked
  10. Promoted draft JSON updated with VERIFIED status
  11. Corrupted draft JSON → skipped silently, no crash
  12. reverify_drafts() never raises even on unexpected errors
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import DraftStatus, Story, StoryRejected, VerificationStatus
from pipeline.draft_reverifier import (
    MAX_AGE_HOURS,
    MAX_REVERIFY_ATTEMPTS,
    _REVERIFY_KEY,
    reverify_drafts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _draft_data(
    draft_status: str = DraftStatus.DRAFT.value,
    verification_status: str = VerificationStatus.UNVERIFIED.value,
    published_hours_ago: float = 2.0,
    attempts: int = 0,
) -> dict:
    return {
        "title": "Test story about an important event",
        "source_name": "BBC",
        "source_url": "https://bbc.com/story",
        "published_at": _ago_iso(published_hours_ago),
        "news_summary": "A test summary of the story.",
        "category": "world",
        "region": "global",
        "draft_status": draft_status,
        "verification_status": verification_status,
        "verification_score": 45.0,
        "verification_reason": "Only one source",
        "corroborating_sources": [],
        _REVERIFY_KEY: attempts,
    }


def _write_draft(path: Path, data: dict) -> Path:
    f = path / "2026-09-13_10-00_test-story.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _verified_story(title: str = "Test story about an important event") -> Story:
    s = Story(
        title=title,
        source_name="BBC",
        source_url="https://bbc.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="A test summary.",
    )
    s.verification_status = VerificationStatus.VERIFIED
    s.verification_score = 84.0
    s.verification_reason = "Confirmed by 2 independent sources."
    return s


def _unverified_story() -> Story:
    s = Story(
        title="Test story about an important event",
        source_name="BBC",
        source_url="https://bbc.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="A test summary.",
    )
    s.verification_status = VerificationStatus.UNVERIFIED
    s.verification_score = 45.0
    return s


# ---------------------------------------------------------------------------
# 1. Empty drafts folder
# ---------------------------------------------------------------------------

def test_empty_drafts_folder_returns_empty(tmp_path):
    with patch("pipeline.draft_reverifier.Path", return_value=tmp_path), \
         patch("config.DRAFTS_DIR", str(tmp_path)):
        result = reverify_drafts()
    assert result == []


# ---------------------------------------------------------------------------
# 2. READY_FOR_REVIEW draft skipped
# ---------------------------------------------------------------------------

def test_ready_for_review_draft_skipped(tmp_path):
    _write_draft(tmp_path, _draft_data(draft_status=DraftStatus.READY_FOR_REVIEW.value))
    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story") as mock_verify:
        result = reverify_drafts()
    assert result == []
    mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# 3. REJECTED draft skipped
# ---------------------------------------------------------------------------

def test_rejected_draft_skipped(tmp_path):
    _write_draft(tmp_path, _draft_data(draft_status=DraftStatus.REJECTED.value))
    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story") as mock_verify:
        result = reverify_drafts()
    assert result == []
    mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Draft older than 48 h skipped
# ---------------------------------------------------------------------------

def test_old_draft_skipped(tmp_path):
    _write_draft(tmp_path, _draft_data(published_hours_ago=MAX_AGE_HOURS + 2))
    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story") as mock_verify:
        result = reverify_drafts()
    assert result == []
    mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Draft with max attempts reached skipped
# ---------------------------------------------------------------------------

def test_max_attempts_reached_skipped(tmp_path):
    _write_draft(tmp_path, _draft_data(attempts=MAX_REVERIFY_ATTEMPTS))
    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story") as mock_verify:
        result = reverify_drafts()
    assert result == []
    mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Draft verified on re-check → promoted and returned
# ---------------------------------------------------------------------------

def test_draft_promoted_when_verified(tmp_path):
    draft_file = _write_draft(tmp_path, _draft_data())
    verified = _verified_story()

    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story", return_value=verified):
        result = reverify_drafts()

    assert len(result) == 1
    assert result[0].verification_status == VerificationStatus.VERIFIED

    # Draft file should be updated
    updated = json.loads(draft_file.read_text(encoding="utf-8"))
    assert updated["verification_status"] == VerificationStatus.VERIFIED.value
    assert updated["draft_status"] == DraftStatus.READY_FOR_REVIEW.value
    assert updated[_REVERIFY_KEY] == 1


# ---------------------------------------------------------------------------
# 7. Draft still single-source → attempt counter incremented, not promoted
# ---------------------------------------------------------------------------

def test_still_single_source_increments_attempts(tmp_path):
    draft_file = _write_draft(tmp_path, _draft_data(attempts=1))
    unverified = _unverified_story()

    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story", return_value=unverified):
        result = reverify_drafts()

    assert result == []
    updated = json.loads(draft_file.read_text(encoding="utf-8"))
    assert updated[_REVERIFY_KEY] == 2  # incremented from 1 to 2


# ---------------------------------------------------------------------------
# 8. StoryRejected on re-check → attempt counter incremented
# ---------------------------------------------------------------------------

def test_story_rejected_increments_attempts(tmp_path):
    draft_file = _write_draft(tmp_path, _draft_data(attempts=0))

    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story", side_effect=StoryRejected("no sources")):
        result = reverify_drafts()

    assert result == []
    updated = json.loads(draft_file.read_text(encoding="utf-8"))
    assert updated[_REVERIFY_KEY] == 1


# ---------------------------------------------------------------------------
# 9. Multiple drafts — only eligible ones checked
# ---------------------------------------------------------------------------

def test_only_eligible_drafts_checked(tmp_path):
    # Draft 1: eligible DRAFT
    (tmp_path / "2026-09-13_10-00_eligible.json").write_text(
        json.dumps(_draft_data()), encoding="utf-8"
    )
    # Draft 2: too old
    (tmp_path / "2026-09-13_10-00_old.json").write_text(
        json.dumps(_draft_data(published_hours_ago=MAX_AGE_HOURS + 5)),
        encoding="utf-8",
    )
    # Draft 3: already READY_FOR_REVIEW
    (tmp_path / "2026-09-13_10-00_ready.json").write_text(
        json.dumps(_draft_data(draft_status=DraftStatus.READY_FOR_REVIEW.value)),
        encoding="utf-8",
    )
    # Draft 4: max attempts
    (tmp_path / "2026-09-13_10-00_maxed.json").write_text(
        json.dumps(_draft_data(attempts=MAX_REVERIFY_ATTEMPTS)),
        encoding="utf-8",
    )

    verified = _verified_story()
    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story", return_value=verified) as mock_v:
        result = reverify_drafts()

    # Only draft 1 was eligible
    assert mock_v.call_count == 1
    assert len(result) == 1


# ---------------------------------------------------------------------------
# 10. Promoted draft JSON updated correctly
# ---------------------------------------------------------------------------

def test_promoted_draft_json_fields_correct(tmp_path):
    draft_file = _write_draft(tmp_path, _draft_data())
    verified = _verified_story()
    verified.verification_score = 88.0
    verified.verification_reason = "Confirmed by 2 independent domains."

    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier.verify_story", return_value=verified):
        reverify_drafts()

    updated = json.loads(draft_file.read_text(encoding="utf-8"))
    assert updated["verification_status"] == VerificationStatus.VERIFIED.value
    assert updated["draft_status"] == DraftStatus.READY_FOR_REVIEW.value
    assert updated["verification_score"] == 88.0
    assert "2 independent" in updated["verification_reason"]


# ---------------------------------------------------------------------------
# 11. Corrupted JSON → skipped silently
# ---------------------------------------------------------------------------

def test_corrupted_json_skipped_silently(tmp_path):
    (tmp_path / "2026-09-13_10-00_corrupt.json").write_text(
        "NOT VALID JSON", encoding="utf-8"
    )
    with patch("config.DRAFTS_DIR", str(tmp_path)):
        result = reverify_drafts()
    assert result == []


# ---------------------------------------------------------------------------
# 12. reverify_drafts() never raises
# ---------------------------------------------------------------------------

def test_reverify_drafts_never_raises(tmp_path):
    with patch("config.DRAFTS_DIR", str(tmp_path)), \
         patch("pipeline.draft_reverifier._run", side_effect=RuntimeError("unexpected")):
        result = reverify_drafts()
    assert result == []
