"""Evidence-based story verification.

Discovery confidence and editorial importance are deliberately separate here.
A story is VERIFIED only when at least two independent, reliable domains report
the same event without a material claim conflict. Strong single-source stories
remain UNVERIFIED so they can be drafted for review without being auto-published.
"""

from __future__ import annotations

import logging

import config
from models import Story, StoryRejected, VerificationStatus
from news.article_extractor import fetch_article
from pipeline.claim_matcher import compare_reports
from pipeline.source_classifier import (
    Confidence,
    SourceTier,
    TIER5_DOMAINS,
    canonical_domain,
    publisher_identity,
)

logger = logging.getLogger(__name__)

MIN_INDEPENDENT_SOURCES = max(2, int(config.NEWS_MIN_SOURCES))
MAX_DEEP_CORROBORATORS = 2


def verify_story(story: Story, deep: bool = False) -> Story:
    """Verify source independence and agreement, optionally reading source pages."""
    domain = canonical_domain(story.source_url)
    source_tier = int(getattr(story, "source_tier", SourceTier.TIER4))

    if domain in TIER5_DOMAINS or source_tier == int(SourceTier.TIER5):
        story.verification_status = VerificationStatus.SPECULATIVE
        story.verification_score = 0.0
        story.verification_reason = f"Unreliable source: {domain or story.source_name}."
        raise StoryRejected(story.verification_reason)

    if getattr(story, "confidence", Confidence.LOW) == Confidence.REJECT:
        story.verification_status = VerificationStatus.SPECULATIVE
        story.verification_score = 0.0
        story.verification_reason = "No reliable primary source is available."
        raise StoryRejected(story.verification_reason)

    if deep:
        _enrich_from_source_pages(story)

    primary_text = story.article_text or story.raw_summary or ""
    evidence: list[dict] = [{
        "name": story.source_name,
        "url": story.source_url,
        "domain": domain,
        "tier": source_tier,
        "role": "primary",
        "independent": True,
        "claim_agreement": 1.0,
        "matched": True,
        "contradictions": [],
    }]

    primary_publisher = publisher_identity(domain)
    seen_publishers = {primary_publisher}
    supporting_domains: set[str] = set()
    material_conflicts: list[str] = []

    for source in story.corroborating_sources or []:
        other_tier = int(source.get("tier", SourceTier.TIER4))
        other_domain = canonical_domain(source.get("url") or source.get("domain", ""))
        reliable = other_tier <= int(SourceTier.TIER3)
        comparison = compare_reports(
            story.title,
            primary_text,
            source.get("title", ""),
            source.get("article_text") or source.get("summary", ""),
            primary_domain=domain,
            other_domain=other_domain,
        )

        independent = (
            reliable
            and bool(other_domain)
            and publisher_identity(other_domain) not in seen_publishers
            and not comparison.syndicated_copy
        )
        if other_domain:
            seen_publishers.add(publisher_identity(other_domain))

        if independent and comparison.matched:
            supporting_domains.add(other_domain)
        if independent and comparison.contradictions:
            material_conflicts.extend(
                f"{source.get('name', other_domain)}: {item}"
                for item in comparison.contradictions
            )

        evidence.append({
            "name": source.get("name", "Unknown"),
            "url": source.get("url", ""),
            "domain": other_domain,
            "publisher_identity": publisher_identity(other_domain),
            "tier": other_tier,
            "role": "corroborating",
            "independent": independent,
            "syndicated_copy": comparison.syndicated_copy,
            "claim_agreement": comparison.agreement,
            "matched": comparison.matched,
            "contradictions": comparison.contradictions,
        })

    story.verification_evidence = evidence
    independent_count = 1 + len(supporting_domains)

    if material_conflicts:
        story.verification_status = VerificationStatus.SPECULATIVE
        story.verification_score = 20.0
        story.verification_reason = "Material conflict across independent sources: " + "; ".join(material_conflicts[:3])
        raise StoryRejected(story.verification_reason)

    if independent_count >= MIN_INDEPENDENT_SOURCES:
        matched_scores = [
            float(item["claim_agreement"])
            for item in evidence[1:]
            if item["independent"] and item["matched"]
        ]
        avg_agreement = sum(matched_scores) / max(len(matched_scores), 1)
        story.verification_status = VerificationStatus.VERIFIED
        story.verification_score = round(
            min(100.0, 65.0 + 15.0 * len(supporting_domains) + 10.0 * avg_agreement),
            1,
        )
        story.verification_reason = (
            f"Confirmed by {independent_count} independent reliable domains; "
            f"average claim agreement {avg_agreement:.0%}."
        )
        logger.info(
            "VERIFIED [%d independent sources | %.0f/100]: %s",
            independent_count,
            story.verification_score,
            story.title[:70],
        )
        return story

    story.verification_status = VerificationStatus.UNVERIFIED
    base_score = {1: 55.0, 2: 45.0, 3: 35.0}.get(source_tier, 20.0)
    story.verification_score = base_score
    story.verification_reason = (
        f"Provisional: {story.source_name} is currently the only independent reliable source. "
        "Keep for review and re-check; do not auto-publish."
    )
    logger.warning("PROVISIONAL [%.0f/100]: %s", story.verification_score, story.title[:70])
    return story


def _enrich_from_source_pages(story: Story) -> None:
    """Read the primary page and up to two corroborators for deeper fact comparison."""
    primary = fetch_article(story.source_url)
    if primary.text:
        story.article_text = primary.text
    elif primary.description and len(primary.description) > len(story.raw_summary or ""):
        story.article_text = primary.description
    if primary.image_url:
        story.article_image_url = primary.image_url

    fetched = 0
    for source in story.corroborating_sources or []:
        if fetched >= MAX_DEEP_CORROBORATORS:
            break
        if int(source.get("tier", SourceTier.TIER4)) > int(SourceTier.TIER3):
            continue
        article = fetch_article(source.get("url", ""))
        fetched += 1
        if article.text:
            source["article_text"] = article.text
            if not story.article_text:
                story.article_text = article.text
        elif article.description and len(article.description) > len(source.get("summary", "")):
            source["article_text"] = article.description
            if not story.article_text:
                story.article_text = article.description


def set_rss_pool(stories: list[Story]) -> None:
    """Compatibility shim; clustering already carries corroborating reports."""
    return None
