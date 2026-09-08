"""
agent.py — Global Pulse News Facebook Publishing Agent

Pipeline:

  STAGE 1  — FETCH NEWS
      ↓      Collect articles from NewsAPI + approved RSS sources
      ↓
  STAGE 1b — SOURCE QUALITY + CLUSTERING
      ↓      Classify every source (Tier 1–5)
      ↓      Group articles reporting the same event into clusters
      ↓      Select highest-tier source as primary per cluster
      ↓      Compute confidence (HIGH / GOOD / LOW / REJECT)
      ↓      Reject: Tier 5 sources, zero-reliable-source clusters
      ↓
  STAGE 2  — REMOVE DUPLICATES
      ↓      URL + title similarity deduplication
      ↓
  STAGE 2b — CONTENT POLICY GATE
      ↓      Policy, safety, content filters
      ↓
  STAGE 3  — VERIFY SOURCES
      ↓      Translate cluster confidence → VerificationStatus
      ↓      REJECT confidence → StoryRejected (never published)
      ↓
  STAGE 4  — EDITORIAL SCORING
      ↓      Score on 6 criteria + category multiplier
      ↓      UNVERIFIED stories capped at 55 (below PUBLISH floor)
      ↓
  STAGE 5  — STORY SELECTION
      ↓      Top stories per category up to requested count
      ↓
  STAGE 6  — BUILD READY-TO-PUBLISH POSTS
      ↓      Generate → Validate → Image → Attention Score → Quality
      ↓      Stories fully built BEFORE entering queue
      ↓
  STAGE 7  — POSTING QUEUE
      ↓      Only fully-built posts enter. Queue = ready to fire.
      ├── BREAKING  → PUBLISH NOW   (score ≥ 88, or Tier 1 ≥ 85)
      ├── HIGH      → NEXT SLOT     (score ≥ 75)
      ├── MEDIUM    → SCHEDULE      (score ≥ 60, 08:00/13:00/19:00 UTC)
      └── LOW/HOLD  → HOLD or REJECT (score < 60)
      ↓
  STAGE 8  — PUBLISH
             Facebook Graph API — verified image-card posts only

Usage:
  python agent.py --publish --image                      # all categories, 1 post each
  python agent.py --publish --image --count 2            # 2 posts per category
  python agent.py --publish --image --category breaking  # single category
  python agent.py --dry-run --category technology        # preview only
  python agent.py --queue-status                         # show queue state
"""

from __future__ import annotations

import argparse
import copy
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
from pipeline.deduplicator import filter_fresh_stories, mark_seen
from pipeline.do_not_publish import DNPDecision, check_do_not_publish, record_published_title
from pipeline.editorial_scorer import EditorialTier, score_and_filter
from pipeline.final_quality_check import FinalQualityError, final_quality_check
from pipeline.generator import generate_post
from pipeline.high_value_backlog import pending_stories, remember, resolve
from pipeline.posting_queue import HARD_DAILY_CEILING, PostingQueue, Route
from pipeline.selector import select_story
from pipeline.clusterer import cluster_stories
from pipeline.verifier import set_rss_pool, verify_story

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
if "--fetch" in sys.argv:
    _log_handlers.append(logging.FileHandler(config.LOGS_DIR / "job1_fetch.log", encoding="utf-8"))
elif "--publish" in sys.argv:
    _log_handlers.append(logging.FileHandler(config.LOGS_DIR / "job2_publish.log", encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

# Seconds to wait between Facebook posts (rate limit protection)
POST_SLEEP_SECONDS = 60


def _select_verified_and_provisional(items: list, per_lane: int) -> list:
    """Keep publish candidates and powerful review candidates independently."""
    verified = [
        item for item in items
        if item[1].verification_status == VerificationStatus.VERIFIED
    ]
    provisional = [
        item for item in items
        if item[1].verification_status != VerificationStatus.VERIFIED
    ]
    return verified[:per_lane] + provisional[:per_lane]


def _preserve_high_impact(scored: list, selected: list) -> list:
    """Keep every consequential update even when a category quota is full."""
    selected_urls = {item[1].source_url for item in selected}
    protected = [
        item for item in scored
        if item[0].impact_score >= 10 and item[1].source_url not in selected_urls
    ]
    if protected:
        logger.info(
            "Impact protection retained %d additional high-impact candidate(s).",
            len(protected),
        )
    return selected + protected


# ---------------------------------------------------------------------------
# JOB 1 — Fetch & Build (fills the queue)
# ---------------------------------------------------------------------------

def fetch_and_build(
    dry_run:        bool = False,
    count:          int  = 1,
    category:       str  = "breaking",
    all_categories: bool = False,
    with_image:     bool = False,
) -> int:
    """
    Run all pipeline stages 1–7.
    Fetches news, clusters, scores, generates posts, builds images,
    and adds fully-built posts to the queue.
    Nothing is published here.
    """
    queue = PostingQueue()

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Global Pulse News — Fetch & Build")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if dry_run:
        logger.info("Mode: DRY RUN — no files written, no posts queued.")

    # Purge stale queue entries
    queue.purge_old(max_age_hours=config.NEWS_MAX_AGE_HOURS)

    # ==========================================================================
    # STAGE 1 — FETCH NEWS
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

    pending = pending_stories()
    if pending:
        raw_urls = {story.source_url for story in raw_stories}
        raw_stories.extend(story for story in pending if story.source_url not in raw_urls)
        logger.info("Restored %d protected high-value candidate(s).", len(pending))

    logger.info("Fetched %d candidate stories total.", len(raw_stories))
    set_rss_pool(raw_stories)

    # ==========================================================================
    # STAGE 1b — SOURCE QUALITY + CLUSTERING
    # ==========================================================================
    logger.info("── STAGE 1b: Source Quality + Clustering ───────")
    clustered_stories = cluster_stories(raw_stories)
    if not clustered_stories:
        logger.warning("No publishable clusters found after source quality filter.")
        return 0
    logger.info(
        "%d events identified from %d articles (avg %.1f sources/event).",
        len(clustered_stories), len(raw_stories),
        sum(getattr(s, "cluster_size", 1) for s in clustered_stories) / max(len(clustered_stories), 1),
    )

    # ==========================================================================
    # STAGE 2 — REMOVE DUPLICATES
    # ==========================================================================
    logger.info("── STAGE 2: Remove Duplicates ──────────────────")
    fresh_stories, dup_count = filter_fresh_stories(
        clustered_stories,
        record_attempts=not dry_run,
    )
    logger.info("%d fresh stories after deduplication (%d duplicates removed).", len(fresh_stories), dup_count)
    if not fresh_stories:
        logger.warning("All stories were duplicates. Nothing to publish.")
        return 0

    # ==========================================================================
    # STAGE 2b — CONTENT POLICY GATE
    # ==========================================================================
    logger.info("── STAGE 2b: Content Policy Gate ───────────────")
    dnp_passed: list = []
    dnp_held:   list = []
    dnp_rejected = 0
    for story in fresh_stories:
        verdict = check_do_not_publish(story)
        if verdict.decision == DNPDecision.PUBLISH:
            dnp_passed.append(story)
        elif verdict.decision == DNPDecision.HOLD:
            dnp_held.append((story, verdict.reason))
        else:
            dnp_rejected += 1
    logger.info("DNP gate: %d passed | %d held | %d rejected.", len(dnp_passed), len(dnp_held), dnp_rejected)
    if not dnp_passed:
        logger.warning("All stories blocked by DNP gate.")
        return 0
    fresh_stories = dnp_passed

    # ==========================================================================
    # STAGE 3 — VERIFY SOURCES
    # ==========================================================================
    logger.info("── STAGE 3: Verify Sources ─────────────────────")
    verification_candidates = []
    rejected_count = 0
    for story in fresh_stories:
        try:
            story = verify_story(story)
            verification_candidates.append(story)
        except StoryRejected as exc:
            logger.debug("Rejected: %s", exc.reason)
            rejected_count += 1
    verified_count = sum(
        1 for story in verification_candidates
        if story.verification_status == VerificationStatus.VERIFIED
    )
    provisional_count = len(verification_candidates) - verified_count
    logger.info(
        "%d stories passed source safety: %d verified | %d provisional | %d rejected.",
        len(verification_candidates), verified_count, provisional_count, rejected_count,
    )
    if not verification_candidates:
        logger.warning("No stories passed verification.")
        return 0

    # ==========================================================================
    # STAGE 4 — EDITORIAL SCORING
    # ==========================================================================
    logger.info("── STAGE 4: Editorial Scoring ──────────────────")
    # A verified HOLD-tier story must remain available for selection even when
    # a higher-scoring provisional story exists. Verification and editorial
    # strength are separate lanes; otherwise provisional breaking content can
    # crowd the only safe publish candidate out of the build stage.
    scored = score_and_filter(
        verification_candidates,
        allow_hold=False,
        preserve_verified_hold=True,
    )
    if not scored:
        logger.info("No strong stories found — allowing HOLD-tier fillers.")
        scored = score_and_filter(verification_candidates, allow_hold=True)
    if not scored:
        logger.warning("All stories scored below 60. Nothing to queue.")
        return 0
    for escore, story in scored:
        if escore.total >= 80 or escore.impact_score >= 10:
            remember(story, escore.total, escore.impact_score)
    logger.info(
        "Score distribution: %s",
        " | ".join(
            f"{tier.value}: {sum(1 for s, _ in scored if s.tier == tier)}"
            for tier in EditorialTier
            if any(s.tier == tier for s, _ in scored)
        ),
    )

    # ==========================================================================
    # STAGE 5 — STORY SELECTION
    # ==========================================================================
    logger.info("── STAGE 5: Story Selection ────────────────────")

    if all_categories:
        from collections import defaultdict
        by_cat: dict[str, list] = defaultdict(list)
        for escore, story in scored:
            cat = getattr(story, "category", "breaking")
            by_cat[cat].append((escore, story))
        selected_scored = [
            item
            for items in by_cat.values()
            for item in _select_verified_and_provisional(items, count)
        ]
    else:
        selected_scored = _select_verified_and_provisional(scored, count)

    selected_scored = _preserve_high_impact(scored, selected_scored)

    selected_verified = sum(
        1 for _, story in selected_scored
        if story.verification_status == VerificationStatus.VERIFIED
    )
    logger.info(
        "Selected %d stories: %d verified publish candidate(s), %d provisional review candidate(s) (tiers: %s).",
        len(selected_scored), selected_verified, len(selected_scored) - selected_verified,
        ", ".join(f"{s.tier.value}" for s, _ in selected_scored),
    )

    # ==========================================================================
    # STAGE 6 — BUILD READY-TO-PUBLISH POSTS
    # ==========================================================================
    logger.info("── STAGE 6: Build Posts (Generate → Validate → Image → Quality) ──")
    ready_posts: list[tuple] = []
    review_drafts = 0

    for escore, story in selected_scored:
        if dry_run and story.verification_status != VerificationStatus.VERIFIED:
            review_drafts += 1
            logger.info(
                "📝 [DRY RUN] Provisional review candidate [%.1f]: %s | %s",
                escore.total, story.title[:60], story.verification_reason,
            )
            continue

        if not dry_run:
            # Deep verification is intentionally delayed until after ranking so
            # only selected stories trigger full article downloads.
            try:
                story = verify_story(story, deep=True)
            except StoryRejected as exc:
                logger.warning("Deep verification rejected '%s': %s", story.title[:50], exc.reason)
                story.draft_status = DraftStatus.REJECTED
                story.rejection_reason = exc.reason
                save_draft(story)
                continue

            # 6a — Generate
            try:
                story = generate_post(story)
            except GenerationError as exc:
                logger.error("Post generation failed for '%s': %s", story.title[:50], exc)
                story.draft_status = DraftStatus.REJECTED
                story.rejection_reason = str(exc)
                save_draft(story)
                continue

            # 6b — Validate
            try:
                story = validate_post(story)
            except ContentValidationError as exc:
                logger.warning("Content validation failed for '%s': %s", story.title[:50], exc)
                story.draft_status = DraftStatus.REJECTED
                story.rejection_reason = str(exc)
                save_draft(story)
                continue

            # Preserve powerful single-source stories as reviewable drafts, but
            # never place them in the automatic Facebook publishing queue.
            if story.verification_status != VerificationStatus.VERIFIED:
                story.draft_status = DraftStatus.DRAFT
                story.rejection_reason = None
                save_draft(story)
                review_drafts += 1
                logger.info(
                    "📝 Provisional story saved for review [editorial %.1f | verification %.1f]: %s",
                    escore.total,
                    story.verification_score,
                    story.title[:60],
                )
                continue

        # 6c — Image
        image_path: Path | None = None
        if with_image and not dry_run:
            try:
                from image.maker import create_news_image
                image_path = create_news_image(story)
                if not image_path:
                    logger.warning("Image creation returned None — building branded fallback.")
                    from image.maker import create_fallback_card
                    image_path = create_fallback_card(story)
            except Exception as exc:
                logger.warning("Image creation failed (%s) — building branded fallback.", exc)
                try:
                    from image.maker import create_fallback_card
                    image_path = create_fallback_card(story)
                except Exception as fallback_exc:
                    logger.error("Branded fallback image failed: %s", fallback_exc)

        if not dry_run and not image_path:
            story.draft_status = DraftStatus.DRAFT
            story.rejection_reason = "A relevant image card is required before publishing."
            save_draft(story)
            logger.warning("📝 Image required — story retained as draft: %s", story.title[:60])
            continue

        # 6d — Attention score gate
        if not dry_run:
            try:
                from image.attention_score import score_attention
                attn = score_attention(story, image_path=image_path, mobile_issues=None)
                logger.info("\n%s", attn.scorecard())
                tier_val = str(getattr(escore, "effective_tier_num", ""))
                is_high  = (story.category or "").lower() in {"breaking", "war", "politics", "world", "crime"} or tier_val == "1"
                if attn.verdict in {"REGENERATE", "IMPROVE"}:
                    logger.warning(
                        "Attention score %d/100 — improvement noted; story remains eligible.",
                        attn.total,
                    )
                elif attn.verdict == "PUBLISH_IF_HIGH" and not is_high:
                    logger.info("Attention score %d/100 — continuing with mandatory card.", attn.total)
                logger.info("✅ Attention score %d/100 — %s", attn.total, attn.verdict)
            except Exception as exc:
                logger.warning("Attention score check failed (%s) — continuing.", exc)

        # 6e — Final DNP check
        if not dry_run:
            final_dnp = check_do_not_publish(story, editorial_score=escore.total, image_query=story.title if image_path else None)
            if final_dnp.decision == DNPDecision.REJECT:
                logger.warning("⛔ Late DNP REJECT [%s]: %s", final_dnp.check_name, final_dnp.reason)
                story.draft_status = DraftStatus.REJECTED
                story.rejection_reason = final_dnp.reason
                save_draft(story)
                continue

        # 6f — Final quality check
        if not dry_run:
            try:
                final_quality_check(story, image_path=image_path)
            except FinalQualityError as exc:
                logger.warning("Final quality check failed for '%s': %s", story.title[:50], exc)
                story.draft_status = DraftStatus.REJECTED
                story.rejection_reason = str(exc)
                save_draft(story)
                continue

        if not dry_run:
            # Persist the evidence-rich verified draft before it enters the
            # queue. Permanent duplicate history is recorded only after the
            # Facebook API confirms publication.
            save_draft(story)

        ready_posts.append((escore, story, image_path))
        logger.info("✅ Ready: [%.1f — %s] %s", escore.total, escore.tier.value, story.title[:60])

    if not ready_posts:
        if review_drafts:
            logger.info("No auto-publishable stories; %d provisional draft(s) saved for review.", review_drafts)
        else:
            logger.warning("No stories passed all build stages. Nothing to queue.")
        return 0

    logger.info("%d/%d stories fully built and ready.", len(ready_posts), len(selected_scored))

    # ==========================================================================
    # STAGE 7 — ADD TO POSTING QUEUE
    # ==========================================================================
    logger.info("── STAGE 7: Posting Queue ──────────────────────")
    expired = queue.expire_stale()
    if expired:
        logger.info("⬇️  %d queue entries expired or downgraded (TTL).", expired)

    immediate_posts = [
        item for item in ready_posts
        if item[0].total >= 80 or item[0].impact_score >= 10
    ]
    scheduled_posts = [item for item in ready_posts if item not in immediate_posts]

    if dry_run:
        logger.info("── DRY RUN: Pipeline preview ───────────────────")
        for i, (escore, story, _) in enumerate(ready_posts, 1):
            icon = "🔴" if escore.tier.value == "PRIORITY / BREAKING" else \
                   "🟠" if escore.tier.value == "HIGH PRIORITY" else \
                   "🟢" if escore.tier.value == "PUBLISH" else "🟡"
            action = "PUBLISH IMMEDIATELY" if (escore.total >= 80 or escore.impact_score >= 10) else "NEXT SCHEDULE"
            logger.info("%s [DRY RUN] %d. [%s | %.1f] %s [%s]",
                icon, i, action, escore.total, story.title[:60], story.source_name)
        return 0

    direct_published = 0
    direct_failed = 0
    if immediate_posts:
        from facebook.publisher import publish_post_with_image
        for escore, story, image_path in immediate_posts:
            try:
                post_id = publish_post_with_image(story, Path(image_path))
                queue.record_direct_publish(
                    story, escore.total, escore.effective_tier_num,
                    Path(image_path), post_id,
                )
                record_published_title(story.title)
                mark_seen(story, permanent=True)
                resolve(story.source_url)
                direct_published += 1
                logger.info("✅ Published immediately [%.1f]: %s", escore.total, story.title[:60])
            except Exception as exc:
                # Preserve the story for an automatic retry if Facebook is
                # temporarily unavailable; normal high-value flow never waits.
                logger.error("Immediate publish failed; queued for retry: %s", exc)
                queue.add(
                    story, escore.total,
                    effective_tier=escore.effective_tier_num,
                    image_path=image_path,
                    post_content=story.post_content,
                    card_headline=story.card_headline,
                    hashtags=story.hashtags,
                )
                direct_failed += 1

    added = 0
    for escore, story, image_path in scheduled_posts:
        queue.add(
            story, escore.total,
            effective_tier = escore.effective_tier_num,
            image_path     = image_path,
            post_content   = getattr(story, "post_content", None),
            card_headline  = getattr(story, "card_headline", None),
            hashtags       = getattr(story, "hashtags", None),
        )
        resolve(story.source_url)
        added += 1

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(
        "Fetch & Build done. %d published immediately; %d scheduled; %d immediate retries; %d provisional drafts.",
        direct_published, added, direct_failed, review_drafts,
    )
    logger.info("Queue: %s", queue.summary())

    logger.info("Run  python agent.py --publish  to fire them.")

    return 0


# ---------------------------------------------------------------------------
# JOB 2 — Publish from queue (instant — no fetch, no generation)
# ---------------------------------------------------------------------------

def publish_from_queue(force_now: bool = False, count: int = 0) -> int:
    """
    Read the queue and publish ready posts via Facebook Graph API.
    No fetching, no generation, no image building — just API calls.
    count=0 means publish all that pass the editorial gate right now.
    """
    from facebook.token_validator import validate_token_or_exit
    validate_token_or_exit(exit_on_failure=True)

    queue = PostingQueue()

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Global Pulse News — Publish from Queue")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Expire stale entries first
    expired = queue.expire_stale()
    if expired:
        logger.info("⬇️  %d queue entries expired or downgraded (TTL).", expired)

    queued = [e for e in queue._entries if e.status == "QUEUED"]
    if not queued:
        logger.warning("Queue is empty. Run  python agent.py --fetch --image  first.")
        return 0

    logger.info("Queue: %s", queue.summary())

    # Hard ceiling check — PUBLISH_NOW stories bypass this
    has_breaking = any(e.route == Route.PUBLISH_NOW.value for e in queued)
    if not queue.can_publish_today() and not has_breaking:
        logger.warning("Hard safety ceiling (%d) reached. Nothing published.", HARD_DAILY_CEILING)
        return 0

    from facebook.publisher import (
        FacebookPublishError,
        FatalPublishError,
        publish_post,
        publish_post_with_image,
    )

    published_count  = 0
    failed_count     = 0
    last_published_at: datetime | None = None
    last_breaking_at:  datetime | None = queue.last_breaking_published_at()

    # Sort queue: highest priority first
    queued.sort(key=lambda e: (e.route_enum.priority, -e.score))

    publish_limit = count if count > 0 else len(queued)

    for entry in queued:
        if published_count >= publish_limit:
            break

        if not queue.can_publish_today() and entry.route != Route.PUBLISH_NOW.value:
            logger.warning("Hard safety ceiling reached mid-run. Stopping.")
            break

        # Defense in depth: legacy queue entries and provisional stories must
        # never reach the Facebook publisher without the new evidence gate.
        if entry.verification_status != VerificationStatus.VERIFIED.value:
            reason = (
                entry.verification_reason
                or "Independent-source verification is required before publishing."
            )
            logger.warning("📝 Review required — not publishing: %s | %s", entry.title[:60], reason)
            queue.mark_review_required(entry, reason)
            continue

        if force_now:
            ok, reason = True, "Explicit --force-now override"
        else:
            ok, reason = queue.deserves_publishing(entry, last_published_at, last_breaking_at)

        route_icon = {
            "PUBLISH_NOW": "🔴 BREAKING",
            "NEXT_SLOT":   "🟠 NEXT SLOT",
            "SCHEDULE":    "🟡 SCHEDULE",
            "HOLD":        "⚪ HOLD",
        }.get(entry.route, entry.route)

        if not ok:
            logger.info("⏸  [%s] %s | %s", route_icon, entry.title[:50], reason)
            continue

        logger.info("▶  [%s | %.1f] %s", route_icon, entry.score, entry.title[:60])

        # Reconstruct story for publishing from queue entry data
        from models import Story
        from datetime import datetime as dt
        story = Story(
            title          = entry.title,
            source_name    = entry.source_name,
            source_url     = entry.source_url,
            published_at   = dt.now(timezone.utc),
            raw_summary    = "",
            post_content   = entry.post_content,
            card_headline  = entry.card_headline,
            hashtags       = entry.hashtags or [],
            category       = entry.category,
            draft_status   = DraftStatus.READY_FOR_REVIEW,
            verification_status = VerificationStatus.VERIFIED,
            verification_score  = entry.verification_score,
            verification_reason = entry.verification_reason,
        )

        image_path = Path(entry.image_path) if entry.image_path else None
        if not image_path or not image_path.exists():
            reason = "A valid image card is required before publishing."
            logger.warning("📝 Review required — %s: %s", reason, entry.title[:60])
            queue.mark_review_required(entry, reason)
            continue

        if not story.post_content:
            logger.warning("No post content in queue entry for '%s' — skipping.", entry.title[:50])
            queue.mark_skipped(entry, "no post content in queue entry")
            failed_count += 1
            continue

        # The evidence-rich draft was already saved during the fetch stage.

        try:
            post_id = publish_post_with_image(story, image_path)
            logger.info("✅ [%d] Published with image. Post ID: %s | %s",
                published_count + 1, post_id, entry.title[:60])

            queue.mark_published(entry, post_id=post_id)
            record_published_title(entry.title)
            mark_seen(story, permanent=True)
            last_published_at = datetime.now(timezone.utc)
            if entry.is_breaking:
                last_breaking_at = last_published_at
            published_count += 1

            # Clean up image file after successful publish
            if image_path and image_path.exists():
                try:
                    image_path.unlink()
                    logger.debug("🗑️  Image deleted after publish: %s", image_path.name)
                except Exception as exc:
                    logger.warning("Could not delete image %s: %s", image_path.name, exc)

            if published_count < publish_limit:
                logger.info("Sleeping %ds before next post...", POST_SLEEP_SECONDS)
                time.sleep(POST_SLEEP_SECONDS)

        except FatalPublishError as exc:
            logger.error("\n%s", exc)
            logger.error("❌ Fatal error — stopping. Refresh your token.\n"
                         "   Published %d post(s) before error.", published_count)
            return 1

        except FacebookPublishError as exc:
            logger.error("Facebook publish failed: %s", exc)
            queue.mark_skipped(entry, f"publish error: {exc}")
            failed_count += 1

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("Done. %d published. %d failed. Queue: %s",
                published_count, failed_count, queue.summary())
    return 0 if failed_count == 0 else 1


# ---------------------------------------------------------------------------
# Legacy combined entry point (--publish --fetch for old muscle memory)
# ---------------------------------------------------------------------------

def main(
    dry_run:        bool = False,
    fetch:          bool = False,
    publish:        bool = False,
    count:          int  = 1,
    category:       str  = "breaking",
    all_categories: bool = False,
    with_image:     bool = False,
    queue_status:   bool = False,
    force_now:      bool = False,
    engagement_report: bool = False,
) -> int:

    queue = PostingQueue()

    if engagement_report:
        from facebook.engagement_tracker import collect_published_metrics
        snapshots = collect_published_metrics(queue._entries)
        logger.info("Engagement report updated for %d Facebook post(s).", len(snapshots))
        return 0

    if queue_status:
        _print_queue_status(queue)
        return 0

    # --publish only (no --fetch) → just fire the queue
    if publish and not fetch and not dry_run:
        return publish_from_queue(force_now=force_now, count=count)

    # --publish --dry-run (no --fetch) → preview queue without publishing
    if publish and not fetch and dry_run:
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info("  Global Pulse News — Publish Queue [DRY RUN]")
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        queue = copy.deepcopy(PostingQueue())
        # Preview queue transitions without touching runtime files or audit logs.
        queue._save = lambda: None
        queue._audit = lambda *args, **kwargs: None
        queue.expire_stale()
        queued = [e for e in queue._entries if e.status == "QUEUED"]
        queued.sort(key=lambda e: (e.route_enum.priority, -e.score))
        logger.info("Queue after TTL checks: %s", queue.summary())
        if not queued:
            logger.info("Queue is empty — nothing to publish.")
        else:
            publishable = []
            skipped = []
            last_published_at = None
            last_breaking_at = queue.last_breaking_published_at()
            for entry in queued:
                if entry.verification_status != VerificationStatus.VERIFIED.value:
                    skipped.append((
                        entry,
                        entry.verification_reason
                        or "Independent-source verification is required.",
                    ))
                    continue
                if not entry.image_path or not Path(entry.image_path).exists():
                    skipped.append((entry, "A valid image card is required before publishing."))
                    continue
                ok, reason = queue.deserves_publishing(
                    entry, last_published_at, last_breaking_at
                )
                if not ok:
                    skipped.append((entry, reason))
                    continue
                publishable.append((entry, reason))
                simulated_at = datetime.now(timezone.utc)
                entry.status = "PUBLISHED"
                entry.published_at = simulated_at.isoformat()
                last_published_at = simulated_at
                if entry.is_breaking:
                    last_breaking_at = simulated_at

            logger.info("Would publish %d post(s) if the publish job ran now:", len(publishable))
            for i, (entry, reason) in enumerate(publishable, 1):
                logger.info(
                    "  %d. [%.1f — %s] %s [%s] | %s",
                    i, entry.score, entry.route, entry.title[:70], entry.source_name, reason,
                )
            logger.info("Would leave %d queued due to timing/editorial gates.", len(skipped))
        return 0

    # --fetch (with or without --publish) → run the full pipeline
    if fetch or dry_run:
        result = fetch_and_build(
            dry_run        = dry_run,
            count          = count,
            category       = category,
            all_categories = all_categories,
            with_image     = with_image,
        )
        if result != 0 or not publish or dry_run:
            return result
        # --fetch --publish: after building, also publish
        return publish_from_queue(force_now=force_now, count=count)

    # Neither --fetch nor --publish → show help hint
    logger.info("Nothing to do. Use --fetch to fill queue, --publish to fire it.")
    logger.info("Run  python agent.py --help  for usage.")
    return 0
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

    # Register RSS pool for verification fallback (kept for backwards compat, no-op now)
    set_rss_pool(raw_stories)

    # ==========================================================================
    # STAGE 1b — SOURCE QUALITY FILTER + STORY CLUSTERING
    # Classify every source (Tier 1–5), group same-event articles into clusters,
    # select best primary source per cluster, compute confidence score.
    # Tier 5 sources and zero-reliable-source clusters are rejected here.
    # ==========================================================================
    logger.info("── STAGE 1b: Source Quality + Clustering ───────")
    clustered_stories = cluster_stories(raw_stories)

    if not clustered_stories:
        logger.warning("No publishable clusters found after source quality filter.")
        return 0

    logger.info(
        "%d events identified from %d articles (avg %.1f sources/event).",
        len(clustered_stories),
        len(raw_stories),
        sum(getattr(s, "cluster_size", 1) for s in clustered_stories) / max(len(clustered_stories), 1),
    )

    # ==========================================================================
    # STAGE 2 — REMOVE DUPLICATES
    # ==========================================================================
    logger.info("── STAGE 2: Remove Duplicates ──────────────────")
    fresh_stories = []
    dup_count = 0
    for story in clustered_stories:
        try:
            check_duplicate(story)
            fresh_stories.append(story)
            # Mark seen immediately — increments attempt counter (TTL=7 days).
            # Stops unverified/held stories from looping run after run.
            # Published stories are upgraded to permanent=True after Stage 11.
            if not dry_run:
                mark_seen(story, permanent=False)
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
    # Translates cluster confidence → VerificationStatus.
    # REJECT confidence and Tier 5 sources raise StoryRejected here.
    # ==========================================================================
    logger.info("── STAGE 3: Verify Sources ─────────────────────")
    verified_stories = []
    rejected_count = 0
    for story in fresh_stories:
        try:
            story = verify_story(story)
            verified_stories.append(story)
        except StoryRejected as exc:
            logger.debug("Rejected: %s", exc.reason)
            rejected_count += 1

    logger.info(
        "%d stories passed verification (%d rejected).",
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
    # STAGE 6 — BUILD READY-TO-PUBLISH POSTS (generate → validate → image → quality)
    # ==========================================================================
    logger.info("── STAGE 6: Build Posts (Generate → Validate → Image → Quality) ──")

    ready_posts: list[tuple] = []  # (escore, story, image_path)

    for escore, story in selected_scored:

        # ------------------------------------------------------------------
        # STAGE 6a — AI POST WRITING
        # ------------------------------------------------------------------
        if not dry_run:
            try:
                story = generate_post(story)
            except GenerationError as exc:
                logger.error("Post generation failed for '%s': %s", story.title[:50], exc)
                story.draft_status    = DraftStatus.REJECTED
                story.rejection_reason = str(exc)
                save_draft(story)
                continue

            # ------------------------------------------------------------------
            # STAGE 6b — CONTENT VALIDATION
            # ------------------------------------------------------------------
            try:
                story = validate_post(story)
            except ContentValidationError as exc:
                logger.warning("Content validation failed for '%s': %s", story.title[:50], exc)
                story.draft_status    = DraftStatus.REJECTED
                story.rejection_reason = str(exc)
                save_draft(story)
                continue

        # ------------------------------------------------------------------
        # STAGE 6c — VISUAL DESIGN (image card)
        # ------------------------------------------------------------------
        image_path: Path | None = None
        mobile_issues: dict     = {}
        if with_image and not dry_run:
            try:
                from image.maker import create_news_image
                image_path = create_news_image(story)
                if not image_path:
                    logger.warning("Image creation returned None for '%s' — will publish text-only.", story.title[:50])
            except Exception as exc:
                logger.warning("Image creation failed (%s) — publishing text-only.", exc)

        # ------------------------------------------------------------------
        # STAGE 6d — ATTENTION SCORE (internal quality gate)
        # ------------------------------------------------------------------
        if not dry_run:
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
                        continue
                    elif attn.verdict == "IMPROVE":
                        logger.warning(
                            "⚠️  Attention score %d/100 — IMPROVE. Skipping: %s",
                            attn.total, story.title[:60],
                        )
                        continue

                logger.info("✅ Attention score %d/100 — %s", attn.total, attn.verdict)

            except Exception as exc:
                logger.warning("Attention score check failed (%s) — continuing.", exc)

        # ------------------------------------------------------------------
        # STAGE 6e — FINAL DNP CHECK (with score + image)
        # ------------------------------------------------------------------
        if not dry_run:
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
                continue

        # ------------------------------------------------------------------
        # STAGE 6f — FINAL QUALITY CHECK
        # ------------------------------------------------------------------
        if not dry_run:
            try:
                final_quality_check(story, image_path=image_path)
            except FinalQualityError as exc:
                logger.warning("Final quality check failed for '%s': %s", story.title[:50], exc)
                story.draft_status    = DraftStatus.REJECTED
                story.rejection_reason = str(exc)
                save_draft(story)
                continue

        # Story fully validated — mark as seen and add to ready list
        if not dry_run:
            mark_seen(story, permanent=True)

        ready_posts.append((escore, story, image_path))
        logger.info(
            "✅ Ready: [%.1f — %s] %s",
            escore.total, escore.tier.value, story.title[:60],
        )

    if not ready_posts:
        logger.warning("No stories passed all build stages. Nothing to queue.")
        return 0

    logger.info(
        "%d/%d stories fully built and ready to publish.",
        len(ready_posts), len(selected_scored),
    )

    # ==========================================================================
    # STAGE 7 — POSTING QUEUE (only fully-built posts enter here)
    # ==========================================================================
    logger.info("── STAGE 7: Posting Queue ──────────────────────")

    # Expire stale entries / downgrade overdue lanes before adding new stories
    expired = queue.expire_stale()
    if expired:
        logger.info("⬇️  %d queue entries expired or downgraded (TTL).", expired)

    for escore, story, image_path in ready_posts:
        queue.add(
            story,
            escore.total,
            effective_tier=escore.effective_tier_num,
            image_path=image_path,
            post_content=getattr(story, "post_content", None),
            card_headline=getattr(story, "card_headline", None),
            hashtags=getattr(story, "hashtags", None),
        )

    logger.info("Queue state: %s", queue.summary())

    # In dry-run mode, print selected stories and exit
    if dry_run:
        logger.info("── DRY RUN: Pipeline preview ───────────────────")
        for i, (escore, story, _) in enumerate(ready_posts, 1):
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
    # STAGE 8 — PUBLISH (queue is already fully built — just fire the API)
    # ==========================================================================
    logger.info("── STAGE 8: Publish ────────────────────────────")

    target            = len(ready_posts)
    published_count   = 0
    failed_count      = 0
    last_published_at: datetime | None = None
    last_breaking_at:  datetime | None = queue.last_breaking_published_at()

    for escore, story, image_path in ready_posts:

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
            # --force-now: promote SCHEDULE stories to NEXT_SLOT, bypassing window check
            if force_now and entry.route == Route.SCHEDULE.value:
                logger.info("⚡ --force-now: promoting '%s' from SCHEDULE → NEXT_SLOT", story.title[:50])
                entry.route = Route.NEXT_SLOT.value

            ok, reason = queue.deserves_publishing(entry, last_published_at, last_breaking_at)

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
        # Save draft
        # ------------------------------------------------------------------
        draft_path = save_draft(story)

        # ------------------------------------------------------------------
        # FACEBOOK PUBLISHING — image_path already built, just send it
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

                # Clean up image file after successful publish
                if image_path and image_path.exists():
                    try:
                        image_path.unlink()
                        logger.debug("🗑️  Image deleted after publish: %s", image_path.name)
                    except Exception as exc:
                        logger.warning("Could not delete image %s: %s", image_path.name, exc)

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
        epilog="""
Examples:
  python agent.py --fetch --image            Fill queue (all categories)
  python agent.py --publish                  Fire queued posts now (instant)
  python agent.py --publish --force-now      Fire queued posts, bypass window
  python agent.py --fetch --image --publish  Fetch + publish in one run
  python agent.py --dry-run                  Preview fetch without queuing
  python agent.py --queue-status             Show queue
        """,
    )
    parser.add_argument("--fetch",          action="store_true",  help="Fetch news and build posts into queue.")
    parser.add_argument("--publish",        action="store_true",  help="Publish queued posts to Facebook (instant — no fetch).")
    parser.add_argument("--image",          action="store_true",  help="Build image cards during --fetch.")
    parser.add_argument("--dry-run",        action="store_true",  help="Preview --fetch pipeline without writing or queuing.")
    parser.add_argument("--count",          type=int, default=1,
                        help="Posts per category for --fetch, or maximum posts for --publish (default: 1; use 0 for all eligible).")
    parser.add_argument("--category",       type=str, default="breaking", choices=list(CATEGORIES.keys()),
                        help="Single category to fetch (default: all).")
    parser.add_argument("--all-categories", action="store_true",  help="Fetch all 14 categories.")
    parser.add_argument("--queue-status",   action="store_true",  help="Show queue status and exit.")
    parser.add_argument("--force-now",      action="store_true",  help="Bypass scheduled window — publish immediately.")
    parser.add_argument("--engagement-report", action="store_true",
                        help="Collect reactions, comments and shares for published posts.")
    args = parser.parse_args()

    # Backwards compat: --publish --image (old style) → treat as --fetch --image --publish
    if args.publish and args.image and not args.fetch:
        args.fetch = True

    # Default: fetch all categories unless a specific one was requested
    all_cats = args.all_categories or (args.category == "breaking" and not any(
        a.startswith("--category") for a in sys.argv[1:]
    ))

    sys.exit(main(
        dry_run        = args.dry_run,
        fetch          = args.fetch,
        publish        = args.publish,
        count          = args.count,
        category       = args.category,
        all_categories = all_cats,
        with_image     = args.image,
        queue_status   = args.queue_status,
        force_now      = args.force_now,
        engagement_report = args.engagement_report,
    ))
