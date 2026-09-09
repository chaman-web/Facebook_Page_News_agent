"""
output/draft_writer.py — Save a story as a JSON + Markdown draft file.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import config
from models import DraftStatus, Story, VerificationStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def save_draft(story: Story) -> str:
    """
    Determine the draft status, write JSON and Markdown files, and return the file path stem.

    Returns the path to the JSON draft file.
    """
    # Only independently verified stories are ready for review/publishing.
    # Powerful single-source stories remain DRAFT so they are preserved without
    # entering the automatic publishing path.
    if story.draft_status == DraftStatus.REJECTED:
        pass
    elif story.verification_status == VerificationStatus.VERIFIED:
        story.draft_status = DraftStatus.READY_FOR_REVIEW
    else:
        story.draft_status = DraftStatus.DRAFT

    story.generated_at = datetime.now(timezone.utc)

    # Build file paths
    drafts_dir = Path(config.DRAFTS_DIR)
    drafts_dir.mkdir(exist_ok=True)

    timestamp = story.generated_at.strftime("%Y-%m-%d_%H-%M")
    slug = _slugify(story.title)
    stem = f"{timestamp}_{slug}"

    json_path = drafts_dir / f"{stem}.json"
    md_path = drafts_dir / f"{stem}.md"

    # Write files
    _write_json(story, json_path)
    _write_markdown(story, md_path)

    logger.info(
        "Draft saved: %s [status=%s]",
        json_path,
        story.draft_status.value,
    )

    return str(json_path)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_json(story: Story, path: Path) -> None:
    data = {
        "title": story.title,
        "source_name": story.source_name,
        "source_url": story.source_url,
        "published_at": story.published_at.isoformat(),
        "news_summary": story.raw_summary,
        "verification_status": story.verification_status.value,
        "verification_score": story.verification_score,
        "verification_reason": story.verification_reason,
        "verification_evidence": story.verification_evidence,
        "corroborating_sources": story.corroborating_sources,
        "article_text": story.article_text,
        "article_image_url": story.article_image_url,
        "article_image_reuse_permitted": story.article_image_reuse_permitted,
        "post_content": story.post_content,
        "card_headline": story.card_headline,
        "card_description": story.card_description,
        "hashtags": story.hashtags,
        "image_provenance": story.image_provenance,
        "image_credit": story.image_credit,
        "image_is_synthetic": story.image_is_synthetic,
        "draft_status": story.draft_status.value,
        "rejection_reason": story.rejection_reason,
        "generated_at": story.generated_at.isoformat() if story.generated_at else None,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_markdown(story: Story, path: Path) -> None:
    status_banner = {
        DraftStatus.READY_FOR_REVIEW: "✅ READY FOR REVIEW",
        DraftStatus.DRAFT: "📝 DRAFT — Awaiting review",
        DraftStatus.REJECTED: "❌ REJECTED",
    }.get(story.draft_status, story.draft_status.value)

    published = story.published_at.strftime("%Y-%m-%d %H:%M UTC")
    generated = (
        story.generated_at.strftime("%Y-%m-%d %H:%M UTC")
        if story.generated_at
        else "Unknown"
    )

    corroborating_md = ""
    if story.corroborating_sources:
        lines = [
            f"- [{s['name']}]({s['url']})" for s in story.corroborating_sources
        ]
        corroborating_md = "\n**Corroborating sources:**\n" + "\n".join(lines) + "\n"

    rejection_md = ""
    if story.rejection_reason:
        rejection_md = f"\n**Rejection reason:** {story.rejection_reason}\n"

    hashtags_str = " ".join(story.hashtags) if story.hashtags else "_None_"

    post_block = (
        f"> {story.post_content.replace(chr(10), chr(10) + '> ')}"
        if story.post_content
        else "_No post content generated._"
    )

    md = f"""# {story.title}

---

## Status: {status_banner}

| Field | Value |
|---|---|
| Source | [{story.source_name}]({story.source_url}) |
| Published | {published} |
| Verification | {story.verification_status.value} |
| Verification score | {story.verification_score:.0f}/100 |
| Generated | {generated} |

{corroborating_md}{rejection_md}
**Verification reason:** {story.verification_reason or "Not recorded."}

---

## News Summary

{story.raw_summary or "_No summary available._"}

---

## Facebook Post Draft

{post_block}

**Hashtags:** {hashtags_str}

---
_This is an auto-generated draft. Review before publishing._
"""

    path.write_text(md, encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_length: int = 60) -> str:
    """Convert a title to a URL-safe filename slug."""
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text[:max_length]
