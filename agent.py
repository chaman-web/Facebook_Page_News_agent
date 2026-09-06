"""
agent.py — Global Pulse News Facebook Publishing Agent

Pipeline:

  FETCH NEWS
      ↓
  COLLECT MANY CANDIDATES  (700–980 stories, NewsAPI + RSS, all 14 categories)
      ↓
  REMOVE DUPLICATES        (URL + title similarity deduplication)
      ↓
  DO-NOT-PUBLISH GATE      (policy, safety, content filters)
      ↓
  VERIFY SOURCES           (cross-source credibility check)
      ↓
  SCORE EVERY STORY        (editorial score: freshness, importance, source, breaking signal)
      ↓
  SELECT ONLY THE BEST     (top stories per category up to requested count)
      ↓
  CHECK RECENTLY PUBLISHED (queue: daily caps, intervals, audience fatigue)
      ↓
  DECIDE:
      ├── BREAKING  → PUBLISH NOW   (score ≥ 88, or Tier 1 ≥ 85)
      ├── HIGH      → NEXT SLOT     (score ≥ 75)
      ├── MEDIUM    → SCHEDULE      (score ≥ 60, next window 07:30/12:30/19:30)
      └── LOW/HOLD  → HOLD or REJECT (score < 60)
      ↓
  WRITE FACEBOOK POST      (Ollama llama3.2 generates post content)
      ↓
  VALIDATE CONTENT         (length, hashtags, formatting)
      ↓
  CREATE GLOBAL PULSE CARD (1200×1500px, full-bleed photo, brand overlay)
      ↓
  VISUAL QUALITY CHECK     (11 mobile visibility checks — redesign on failure)
      ↓
  ATTENTION SCORE          (AI scores card /100 on 7 criteria — gate before publish)
      ↓
  FINAL CONTENT CHECK      (freshness, policy re-check, near-duplicate, image file)
      ↓
  PUBLISH                  (Facebook Graph API — image post or text-only fallback)

Usage:
  python agent.py --publish --image                      # all categories, 1 post each
  python agent.py --publish --image --count 2            # 2 posts per category
  python agent.py --publish --image --category breaking  # single category
  python agent.py --dry-run --category technology        # preview only
  python agent.py --queue-status                         # show queue state
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config  # noqa: F401 — validates API keys on import

from models import (
    DraftStatus,
    DuplicateStory,
    GenerationError,
    NewsSourceError,
    StoryRejected,
    VerificationStatus,
)
from news.fetcher import CATEGORIES, fetch_all_categories, fetch_news
from output.draft_writer import save_draft
from pipeline.content_validator import ContentValidationError, validate_post
from pipeline.deduplicator import check_duplicate, mark_seen
from pipeline.do_not_publish import DNPDecision, check_do_not_publish, record_published_title
from pipeline.editorial_scorer import EditorialTier, score_and_filter
from pipeline.final_quality_check import FinalQualityError, final_quality_check
from pipeline.generator import generate_post
from pipeline.posting_queue import HARD_DAILY_CEILING, PostingQueue
from pipeline.selector import select_story
from pipeline.verifier import verify_story

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Seconds to wait between Facebook posts (rate limit protection)
POST_SLEEP_SECONDS = 60


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main(
    dry_run:        bool = False,
    publish:        bool = False,
    count:          int  = 1,
    category:       str  = "breaking",
    all_categories: bool = False,
    with_image:     bool = False,
    queue_status:   bool = False,
) -> int:

    queue = PostingQueue()

    # --- Queue status mode ---
    if queue_status:
        _print_queue_status(queue)
        return 0

    # --- Header ---
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Global Pulse News — Facebook Publishing Agent")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if dry_run:
        logger.info("Mode: DRY RUN — no files written, no posts published.")
    elif publish:
        logger.info("Mode: PUBLISH%s", " + IMAGE" if with_image else "")
    else:
        logger.info("Mode: DRAFT ONLY — posts saved but not published.")

    # --- Token pre-validation (fast /me check before any work starts) ---
    if publish and not dry_run:
        from facebook.token_validator import validate_token_or_exit
        validate_token_or_exit(exit_on_failure=True)

    # --- Editorial day status (informational only — not a quota) ---
    if not dry_run and publish:
        published_today = queue.daily_published_count()
        day_type        = queue._classify_day()

        # Only hard ceiling blocks publishing — never a soft target
        if published_today >= HARD_DAILY_CEILING:
            logger.warning(
                "Hard safety ceiling reached (%d posts today). "
                "This is a circuit breaker, not a normal limit.",
                published_today,
            )
            return 0

        logger.info(
            "Today so far: %d posts published | Day type: %s | "
            "The agent will publish any story that deserves it.",
            published_today, day_type,
        )

    # --- Purge stale queue entries ---
    queue.purge_old(max_age_hours=config.NEWS_MAX_AGE_HOURS)

    # ==========================================================================
    # STAGE 1 — FETCH NEWS (collect many candidates)
    # ==========================================================================
    logger.info("── STAGE 1: Fetch News ─────────────────────────")
    try:
        if all_categories:
            logger.info("Fetching ALL %d categories (large pool)...", len(CATEGORIES))
            raw_stories = fetch_all_categories(limit_per_category=max(count * 5, 20))
        else:
            cat_label = CATEGORIES.get(category, {}).get("label", category)
            logger.info("Category: %s", cat_label)
            raw_stories = fetch_news(limit=max(count * 10, 30), category=category)
    except NewsSourceError as exc:
        logger.error("Could not fetch news: %s", exc)
        return 1

    if not raw_stories:
        logger.warning("No stories returned from news sources.")
        return 0

    logger.info("Fetched %d candidate stories total.", len(raw_stories))

    # ==========================================================================
    # STAGE 2 — REMOVE DUPLICATES
    # ==========================================================================
    logger.info("── STAGE 2: Remove Duplicates ──────────────────")
    fresh_stories = []
    dup_count = 0
    for story in raw_stories:
        try:
            check_duplicate(story)
            fresh_stories.append(story)
        except DuplicateStory:
            dup_count += 1

    logger.info(
        "%d fresh stories after deduplication (%d duplicates removed).",
        len(fresh_stories), dup_count,
    )
    if not fresh_stories:
        logger.warning("All stories were duplicates. Nothing to publish.")
        return 0

    # ==========================================================================
    # STAGE 2b — CONTENT POLICY GATE (do-not-publish filter)
    # ==========================================================================
    logger.info("── STAGE 2b: Content Policy Gate ───────────────")
    dnp_passed  : list = []
    dnp_held    : list = []
    dnp_rejected: int  = 0

    for story in fresh_stories:
        verdict = check_do_not_publish(story)
        if verdict.decision == DNPDecision.PUBLISH:
            dnp_passed.append(story)
        elif verdict.decision == DNPDecision.HOLD:
            dnp_held.append((story, verdict.reason))
        else:
            dnp_rejected += 1

    logger.info(
        "DNP gate: %d passed | %d held | %d rejected.",
        len(dnp_passed), len(dnp_held), dnp_rejected,
    )
    if dnp_held:
        logger.info(
            "Held stories: %s",
            " | ".join(f"{s.title[:40]} ({r})" for s, r in dnp_held[:3]),
        )

    if not dnp_passed:
        logger.warning("All stories blocked by DNP gate.")
        return 0

    fresh_stories = dnp_passed

    # ==========================================================================
    # STAGE 3 — VERIFY SOURCES
    # ==========================================================================
    logger.info("── STAGE 3: Verify Sources ─────────────────────")
    verified_stories = []
    rejected_count = 0
    for story in fresh_stories:
        try:
            story = verify_story(story)
            verified_stories.append(story)
        except StoryRejected as exc:
            logger.debug("Rejected (unreliable source): %s", exc.reason)
            rejected_count += 1

    logger.info(
        "%d stories passed verification (%d rejected — unreliable source).",
        len(verified_stories), rejected_count,
    )
    if not verified_stories:
        logger.warning("No stories passed verification.")
        return 0

    # ==========================================================================
    # STAGE 4 — EDITORIAL SCORING
    # ==========================================================================
    logger.info("── STAGE 4: Editorial Scoring ──────────────────")

    # First pass: try without HOLD fillers
    scored = score_and_filter(verified_stories, allow_hold=False)

    # If nothing passes the strong threshold, fall back to HOLD stories
    if not scored:
        logger.info("No strong stories found — allowing HOLD-tier fillers.")
        scored = score_and_filter(verified_stories, allow_hold=True)

    if not scored:
        logger.warning("All stories scored below 60 (DO NOT PUBLISH). Nothing to queue.")
        return 0

    logger.info(
        "Score distribution: %s",
        " | ".join(
            f"{tier.value}: {sum(1 for s, _ in scored if s.tier == tier)}"
            for tier in EditorialTier
            if any(s.tier == tier for s, _ in scored)
        ),
    )

    # ==========================================================================
    # STAGE 5 — STORY SELECTION (best per category up to count)
    # ==========================================================================
    logger.info("── STAGE 5: Story Selection ────────────────────")

    if all_categories:
        from collections import defaultdict
        by_cat: dict[str, list] = defaultdict(list)
        for escore, story in scored:
            cat = getattr(story, "category", "breaking")
            if len(by_cat[cat]) < count:
                by_cat[cat].append((escore, story))
        selected_scored = [item for items in by_cat.values() for item in items]
    else:
        selected_scored = scored[:count]

    logger.info(
        "Selected %d stories (tiers: %s).",
        len(selected_scored),
        ", ".join(f"{s.tier.value}" for s, _ in selected_scored),
    )

    # ==========================================================================
    # STAGE 6 — POSTING QUEUE
    # ==========================================================================
    logger.info("── STAGE 6: Posting Queue ──────────────────────")

    # Expire stale entries / downgrade overdue lanes before adding new stories
    expired = queue.expire_stale()
    if expired:
        logger.info("⬇️  %d queue entries expired or downgraded (TTL).", expired)

    for escore, story in selected_scored:
        queue.add(story, escore.total, effective_tier=escore.effective_tier_num)

    logger.info("Queue state: %s", queue.summary())

    # In dry-run mode, print selected stories and exit
    if dry_run:
        logger.info("── DRY RUN: Pipeline preview ───────────────────")
        for i, (escore, story) in enumerate(selected_scored, 1):
            icon = "🔴" if escore.tier.value == "PRIORITY / BREAKING" else \
                   "🟠" if escore.tier.value == "HIGH PRIORITY" else \
                   "🟢" if escore.tier.value == "PUBLISH" else "🟡"
            logger.info(
                "%s [DRY RUN] %d. [%.1f — %s] %s [%s]",
                icon, i, escore.total, escore.tier.value,
                story.title[:60], story.source_name,
            )
        return 0

    # ==========================================================================
    # STAGES 7–11 — WRITE → VALIDATE → DESIGN → VISUAL CHECK → SCORE → PUBLISH
    # ==========================================================================
    logger.info("── STAGES 7–11: Post pipeline ──────────────────")

    target            = len(selected_scored)   # total stories we intend to process
    published_count   = 0
    failed_count      = 0
    last_published_at: datetime | None = None
    last_breaking_at:  datetime | None = queue.last_breaking_published_at()

    for escore, story in selected_scored:

        # Hard safety ceiling — circuit breaker only
        if not queue.can_publish_today():
            logger.warning(
                "Hard safety ceiling (%d) reached. Stopping as a precaution.",
                HARD_DAILY_CEILING,
            )
            break

        # Find queue entry for this story
        entry = next(
            (e for e in queue._entries
             if e.source_url == story.source_url and e.status == "QUEUED"),
            None,
        )
        if not entry:
            continue

        # The only question: does this story deserve our audience's attention?
        if publish:
            ok, reason = queue.deserves_publishing(entry, last_published_at, last_breaking_at)

            # Log the routing decision clearly
            route_icon = {
                "PUBLISH_NOW": "🔴 BREAKING → PUBLISH NOW",
                "NEXT_SLOT":   "🟠 HIGH     → NEXT SLOT",
                "SCHEDULE":    "🟡 MEDIUM   → SCHEDULE",
                "HOLD":        "⚪ LOW      → HOLD",
            }.get(entry.route, f"⚪ {entry.route}")

            if not ok:
                logger.info("⏸  [%s] %s | %s", route_icon, story.title[:50], reason)
                continue
            else:
                logger.info("▶  [%s] %s", route_icon, story.title[:55])

        # ------------------------------------------------------------------
        # STAGE 7 — AI POST WRITING
        # ------------------------------------------------------------------
        try:
            story = generate_post(story)
        except GenerationError as exc:
            logger.error("Post generation failed: %s", exc)
            story.draft_status    = DraftStatus.REJECTED
            story.rejection_reason = str(exc)
            save_draft(story)
            queue.mark_skipped(entry, "generation failed")
            failed_count += 1
            continue

        # ------------------------------------------------------------------
        # STAGE 8 — CONTENT VALIDATION
        # ------------------------------------------------------------------
        try:
            story = validate_post(story)
        except ContentValidationError as exc:
            logger.warning("Content validation failed: %s", exc)
            story.draft_status    = DraftStatus.REJECTED
            story.rejection_reason = str(exc)
            save_draft(story)
            queue.mark_skipped(entry, "content validation failed")
            failed_count += 1
            continue

        # ------------------------------------------------------------------
        # STAGE 9 — VISUAL DESIGN (image card)
        # ------------------------------------------------------------------
        image_path: Path | None = None
        mobile_issues: dict     = {}
        if with_image and publish:
            try:
                from image.maker import create_news_image
                image_path = create_news_image(story)
                if not image_path:
                    logger.warning("Image creation returned None — will publish text-only.")
            except Exception as exc:
                logger.warning("Image creation failed (%s) — publishing text-only.", exc)

        # ------------------------------------------------------------------
        # STAGE 9a — ATTENTION SCORE (internal quality gate)
        # Ask Ollama to score the card as a human editor would.
        # Gate:  PUBLISH       → proceed normally
        #        PUBLISH_IF_HIGH → proceed only for Tier 1 / breaking
        #        IMPROVE       → skip (mobile checks already tried redesign)
        #        REGENERATE    → skip this story entirely
        # ------------------------------------------------------------------
        if publish and not dry_run:
            try:
                from image.attention_score import (
                    score_attention,
                    THRESHOLD_PUBLISH,
                    THRESHOLD_PUBLISH_IF_HIGH,
                    THRESHOLD_IMPROVE,
                )
                attn = score_attention(story, image_path=image_path,
                                       mobile_issues=mobile_issues or None)
                logger.info("\n%s", attn.scorecard())

                if attn.verdict == "REGENERATE":
                    logger.warning(
                        "⛔ Attention score %d/100 — REGENERATE. Skipping story: %s",
                        attn.total, story.title[:60],
                    )
                    queue.mark_skipped(entry, f"attention score {attn.total}/100: REGENERATE")
                    failed_count += 1
                    continue

                elif attn.verdict in ("IMPROVE", "PUBLISH_IF_HIGH"):
                    tier_val = str(getattr(escore, "effective_tier_num", ""))
                    is_high  = (
                        (story.category or "").lower() in
                            {"breaking", "war", "politics", "world", "crime"}
                        or tier_val == "1"
                    )
                    if attn.verdict == "PUBLISH_IF_HIGH" and not is_high:
                        logger.warning(
                            "⚠️  Attention score %d/100 (PUBLISH_IF_HIGH) but story is "
                            "not high-priority. Skipping: %s",
                            attn.total, story.title[:60],
                        )
                        queue.mark_skipped(
                            entry,
                            f"attention score {attn.total}/100: not high-priority enough",
                        )
                        failed_count += 1
                        continue
                    elif attn.verdict == "IMPROVE":
                        logger.warning(
                            "⚠️  Attention score %d/100 — IMPROVE. "
                            "Mobile checks already exhausted redesign attempts. Skipping: %s",
                            attn.total, story.title[:60],
                        )
                        queue.mark_skipped(entry, f"attention score {attn.total}/100: IMPROVE")
                        failed_count += 1
                        continue

                # PUBLISH or PUBLISH_IF_HIGH for high-priority → fall through
                logger.info(
                    "✅ Attention score %d/100 — %s", attn.total, attn.verdict
                )

            except Exception as exc:
                # Never block publishing over a scoring error
                logger.warning("Attention score check failed (%s) — continuing.", exc)

        # ------------------------------------------------------------------
        # STAGE 9b — FINAL DNP CHECK (with score + image)
        # Run again now that we have the editorial score and image path
        # ------------------------------------------------------------------
        image_query = story.title if image_path else None
        final_dnp   = check_do_not_publish(
            story,
            editorial_score = escore.total,
            image_query     = image_query,
        )
        if final_dnp.decision == DNPDecision.REJECT:
            logger.warning(
                "⛔ Late DNP REJECT [%s]: %s", final_dnp.check_name, final_dnp.reason
            )
            story.draft_status     = DraftStatus.REJECTED
            story.rejection_reason = final_dnp.reason
            save_draft(story)
            queue.mark_skipped(entry, f"late DNP: {final_dnp.check_name}")
            failed_count += 1
            continue

        # ------------------------------------------------------------------
        # STAGE 10 — FINAL QUALITY CHECK
        # ------------------------------------------------------------------
        try:
            final_quality_check(story, image_path=image_path)
        except FinalQualityError as exc:
            logger.warning("Final quality check failed: %s", exc)
            story.draft_status    = DraftStatus.REJECTED
            story.rejection_reason = str(exc)
            save_draft(story)
            queue.mark_skipped(entry, f"final quality: {exc}")
            failed_count += 1
            continue

        # ------------------------------------------------------------------
        # Save draft
        # ------------------------------------------------------------------
        draft_path = save_draft(story)
        mark_seen(story)

        # ------------------------------------------------------------------
        # STAGE 11 — FACEBOOK PUBLISHING
        # ------------------------------------------------------------------
        if publish and story.draft_status == DraftStatus.READY_FOR_REVIEW:
            from facebook.publisher import (
                FacebookPublishError,
                FatalPublishError,
                publish_post,
                publish_post_with_image,
            )
            try:
                if image_path:
                    post_id = publish_post_with_image(story, image_path)
                    logger.info(
                        "✅ [%d/%d] [%s] Published with image. Post ID: %s | %s",
                        published_count + 1, target, escore.tier.value,
                        post_id, story.title[:60],
                    )
                else:
                    post_id = publish_post(story)
                    logger.info(
                        "✅ [%d/%d] [%s] Published (text). Post ID: %s | %s",
                        published_count + 1, target, escore.tier.value,
                        post_id, story.title[:60],
                    )

                queue.mark_published(entry)
                record_published_title(story.title)
                last_published_at = datetime.now(timezone.utc)
                if entry.is_breaking:
                    last_breaking_at = last_published_at
                published_count  += 1

                if published_count < target:
                    logger.info("Sleeping %ds before next post...", POST_SLEEP_SECONDS)
                    time.sleep(POST_SLEEP_SECONDS)

            except FatalPublishError as exc:
                logger.error("\n%s", exc)
                logger.error(
                    "❌ Fatal error — stopping. Refresh your token and try again.\n"
                    "   Published %d/%d posts before error.", published_count, target
                )
                return 1

            except FacebookPublishError as exc:
                logger.error("Facebook publish failed: %s", exc)
                queue.mark_skipped(entry, f"publish error: {exc}")
                failed_count += 1

        elif publish:
            logger.warning(
                "Skipping publish — draft status is %s (need READY_FOR_REVIEW).",
                story.draft_status.value,
            )
            published_count += 1
        else:
            # Draft-only mode — saved above
            logger.info(
                "📝 [%d/%d] Draft saved: %s",
                published_count + 1, target, draft_path,
            )
            published_count += 1

    # --- Summary ---
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(
        "Done. %d/%d published. %d failed. Queue: %s",
        published_count, target, failed_count, queue.summary(),
    )
    return 0 if failed_count == 0 else 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_queue_status(queue: PostingQueue) -> None:
    today    = queue.daily_published_count()
    queued   = queue.queued_count()
    day_type = queue._classify_day()

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Global Pulse News — Queue Status")
    logger.info("  Day type        : %s", day_type)
    logger.info("  Published today : %d  (ceiling: %d)", today, HARD_DAILY_CEILING)
    logger.info("  Currently queued: %d stories", queued)
    logger.info("  %s", queue.summary())
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    entries = [e for e in queue._entries if e.status == "QUEUED"]
    if entries:
        entries.sort(key=lambda e: (e.category_tier, -e.score))
        for i, e in enumerate(entries[:10], 1):
            tier_icon = {1: "🔴", 2: "🟠", 3: "🟡"}.get(e.category_tier, "⚪")
            logger.info(
                "  %s %d. [T%d | %.1f] %-12s %s",
                tier_icon, i, e.category_tier, e.score,
                e.category.upper()[:12], e.title[:60],
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Global Pulse News — Facebook Publishing Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run",        action="store_true",  help="Preview pipeline without writing or publishing.")
    parser.add_argument("--publish",        action="store_true",  help="Publish to Facebook Page.")
    parser.add_argument("--image",          action="store_true",  help="Attach image card to each post.")
    parser.add_argument("--count",          type=int, default=1,  help="Posts per category per run (default: 1).")
    parser.add_argument("--category",       type=str, default="breaking", choices=list(CATEGORIES.keys()),
                        help="Single category to process.")
    parser.add_argument("--all-categories", action="store_true",  help="Process all 14 categories.")
    parser.add_argument("--queue-status",   action="store_true",  help="Show queue status and exit.")
    args = parser.parse_args()

    sys.exit(main(
        dry_run        = args.dry_run,
        publish        = args.publish,
        count          = args.count,
        category       = args.category,
        all_categories = args.all_categories,
        with_image     = args.image,
        queue_status   = args.queue_status,
    ))
