"""
agent.py — Facebook News Agent, Phase 1

Orchestrates the full pipeline:
  fetch → select → deduplicate → verify → generate → save draft

Usage:
  python agent.py            # Normal run
  python agent.py --dry-run  # Run pipeline but do not write any files
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

# config is imported first so it validates API keys before anything else runs
import config  # noqa: F401

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
from pipeline.deduplicator import check_duplicate, mark_seen
from pipeline.generator import generate_post
from pipeline.selector import select_story
from pipeline.verifier import verify_story
from pipeline.content_validator import ContentValidationError, validate_post

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(dry_run: bool = False, publish: bool = False, count: int = 1, category: str = "breaking", all_categories: bool = False, with_image: bool = False) -> int:
    """
    Run the news agent pipeline.
    Processes up to `count` stories per category in one run.
    Returns 0 on success, 1 on unrecoverable error.
    """
    logger.info("=== Facebook News Agent — Phase 1 ===")
    if dry_run:
        logger.info("DRY RUN mode: no files will be written.")
    if publish:
        logger.info("PUBLISH mode: approved drafts will be posted to Facebook.")
    if with_image:
        logger.info("IMAGE mode: posts will include a Pexels news image with headline overlay.")

    # ------------------------------------------------------------------
    # Step 1: Fetch news
    # ------------------------------------------------------------------
    try:
        if all_categories:
            logger.info("Fetching from ALL 8 categories (%d stories each)...", count)
            stories = fetch_all_categories(limit_per_category=count)
        else:
            cat_label = CATEGORIES.get(category, {}).get("label", category)
            logger.info("Category: %s | Target: %d post(s)", cat_label, count)
            stories = fetch_news(limit=max(count * 4, 20), category=category)
    except NewsSourceError as exc:
        logger.error("Could not fetch news: %s", exc)
        return 1

    if not stories:
        logger.warning("No stories returned from news sources. Exiting.")
        return 0

    # ------------------------------------------------------------------
    # Steps 2–7: Process stories until we hit the target count
    # ------------------------------------------------------------------
    published_count = 0
    failed_count = 0
    target = count * len(CATEGORIES) if all_categories else count

    for candidate in stories:
        if published_count >= target:
            break

        # Basic selection check (age, title, URL)
        try:
            story = select_story([candidate])
        except StoryRejected:
            continue

        # Tag story with its category for image labelling
        story.category = category if not all_categories else getattr(candidate, "category", "breaking")

        # Duplicate check
        try:
            check_duplicate(story)
        except DuplicateStory as exc:
            logger.info("Skipping duplicate: %s", exc.reason)
            continue

        # Verify
        try:
            story = verify_story(story)
        except StoryRejected as exc:
            logger.warning("Story rejected during verification: %s", exc.reason)
            story.draft_status = DraftStatus.REJECTED
            story.rejection_reason = exc.reason
            if not dry_run:
                save_draft(story)
            continue

        # Generate post
        try:
            story = generate_post(story)
        except GenerationError as exc:
            logger.error("Post generation failed: %s", exc)
            story.draft_status = DraftStatus.REJECTED
            story.rejection_reason = str(exc)
            if not dry_run:
                save_draft(story)
            failed_count += 1
            continue

        # Validate generated content
        try:
            story = validate_post(story)
        except ContentValidationError as exc:
            logger.warning("Content validation failed: %s", exc)
            story.draft_status = DraftStatus.REJECTED
            story.rejection_reason = str(exc)
            if not dry_run:
                save_draft(story)
            failed_count += 1
            continue

        # Save draft
        if not dry_run:
            path = save_draft(story)
            mark_seen(story)
            logger.info(
                "✓ Story %d/%d — Status: %s | File: %s",
                published_count + 1,
                target,
                story.draft_status.value,
                path,
            )

            # Publish to Facebook
            if publish and story.draft_status.value == "READY_FOR_REVIEW":
                from facebook.publisher import FacebookPublishError, FatalPublishError, publish_post, publish_post_with_image
                try:
                    if with_image:
                        from image.maker import create_news_image
                        image_path = create_news_image(story)
                        if image_path:
                            post_id = publish_post_with_image(story, image_path)
                            logger.info("✓ Published to Facebook with image. Post ID: %s", post_id)
                        else:
                            logger.warning("Image creation failed — publishing text-only post instead.")
                            post_id = publish_post(story)
                            logger.info("✓ Published to Facebook (text only). Post ID: %s", post_id)
                    else:
                        post_id = publish_post(story)
                        logger.info("✓ Published to Facebook. Post ID: %s", post_id)
                    published_count += 1
                    # Sleep between posts to avoid rate limiting
                    if published_count < target:
                        logger.info("Sleeping 60 seconds before next post...")
                        time.sleep(60)
                except FatalPublishError as exc:
                    logger.error("\n%s", exc)
                    logger.error("❌ Stopping all publishing. Please refresh your token and try again.")
                    logger.info("=== Stopped early. %d/%d post(s) published before error. ===", published_count, target)
                    return 1
                except FacebookPublishError as exc:
                    logger.error("Facebook publish failed: %s", exc)
                    failed_count += 1
            elif publish:
                logger.warning(
                    "Skipping publish — draft status is %s (must be READY_FOR_REVIEW).",
                    story.draft_status.value,
                )
                published_count += 1
            else:
                published_count += 1
        else:
            logger.info("[DRY RUN] Story %d/%d: %s", published_count + 1, target, story.title)
            logger.info("[DRY RUN] Status: %s", story.verification_status.value)
            logger.info("[DRY RUN] Post preview:\n%s", story.post_content)
            logger.info("[DRY RUN] Hashtags: %s", " ".join(story.hashtags))
            published_count += 1

    logger.info(
        "=== Done. %d/%d post(s) processed. %d failed. ===",
        published_count,
        target,
        failed_count,
    )
    return 0 if failed_count == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook News Agent — Phase 1")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline without writing any files.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish READY_FOR_REVIEW drafts to Facebook Page (requires FACEBOOK_PAGE_TOKEN in .env).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of posts to process per category in one run (default: 1).",
    )
    parser.add_argument(
        "--category",
        type=str,
        default="breaking",
        choices=list(CATEGORIES.keys()),
        help="News category to fetch (default: breaking). Choices: " + ", ".join(CATEGORIES.keys()),
    )
    parser.add_argument(
        "--all-categories",
        action="store_true",
        help="Fetch and publish from all 8 categories (uses --count per category).",
    )
    parser.add_argument(
        "--image",
        action="store_true",
        help="Step 2: Attach a relevant Pexels image with headline overlay to each post.",
    )
    args = parser.parse_args()
    sys.exit(main(
        dry_run=args.dry_run,
        publish=args.publish,
        count=args.count,
        category=args.category,
        all_categories=args.all_categories,
        with_image=args.image,
    ))
