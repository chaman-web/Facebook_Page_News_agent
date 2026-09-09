"""Context-aware Meta policy checks for generated Facebook news posts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import config
from models import Story
from pipeline.observability import RUN_ID

logger = logging.getLogger(__name__)

POLICY_VERSION = "2026-09"
APPROVED_IMAGE_PROVENANCE = {
    "pexels",
    "licensed_article_image",
    "branded_fallback",
    "pollinations_ai",
}


class PolicyDecision(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class PolicyVerdict:
    decision: PolicyDecision
    categories: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


_NEWS_CONTEXT = re.compile(
    r"\b(?:according to|authorities|officials?|police|reported|reportedly|"
    r"investigat(?:e|es|ed|ing|ion)|condemn(?:s|ed)?|court|prosecutors?|witnesses?)\b",
    re.IGNORECASE,
)

_UNAMBIGUOUS_BLOCKS: tuple[tuple[str, str, str], ...] = (
    (
        "violence_incitement",
        r"\b(?:everyone|people|we|you)\s+(?:should|must|need to)\s+"
        r"(?:kill|shoot|attack|bomb|burn|execute)\b",
        "Direct encouragement of violence.",
    ),
    (
        "self_harm_instruction",
        r"\b(?:how to|best way to|instructions? (?:to|for))\s+"
        r"(?:commit suicide|kill yourself|self[- ]harm)\b",
        "Instructions encouraging suicide or self-harm.",
    ),
    (
        "child_sexual_exploitation",
        r"\b(?:buy|sell|share|download|trade)\b.{0,40}\b"
        r"(?:child sexual|underage sexual|minor sexual)\b",
        "Sexual exploitation involving minors.",
    ),
    (
        "fraud",
        r"\b(?:send|pay|deposit|transfer)\b.{0,50}\b(?:guaranteed returns?|double your money|free money)\b",
        "Fraudulent financial solicitation.",
    ),
)

_REVIEW_SIGNALS: tuple[tuple[str, str, str], ...] = (
    (
        "graphic_violence",
        r"\b(?:dismembered|decapitated|charred bod(?:y|ies)|blood[- ]soaked|"
        r"severed (?:head|limb)|graphic footage|corpse(?:s)?)\b",
        "Potentially graphic description of injury or death.",
    ),
    (
        "dangerous_organizations",
        r"\b(?:join|support|praise|glory to|donate to)\b.{0,50}\b"
        r"(?:terrorist|militant|extremist)\b",
        "Possible praise or support for a dangerous organization.",
    ),
    (
        "medical_misinformation",
        r"\b(?:miracle cure|guaranteed cure|doctors? (?:are )?hiding|"
        r"vaccines? cause|stop taking (?:your )?medicine)\b",
        "Potentially harmful or unsupported medical claim.",
    ),
    (
        "privacy",
        r"\b(?:home address|personal phone number|passport number|national id|"
        r"social security number)\b\s*(?:is|:)",
        "Potential exposure of private identifying information.",
    ),
    (
        "engagement_bait",
        r"\b(?:share this(?: post)? if|like this if|tag (?:a|your) friend|"
        r"comment ['\"]?\w+['\"]? if)\b",
        "Explicit engagement bait may reduce Page distribution.",
    ),
)

_HATE_TARGET = re.compile(
    r"\b(?:race|ethnic(?:ity)?|religion|muslims?|jews?|christians?|hindus?|"
    r"women|men|gay|lesbian|transgender|disabled|immigrants?|refugees?|nationality)\b",
    re.IGNORECASE,
)
_HATE_ATTACK = re.compile(
    r"\b(?:animals?|vermin|disease|subhuman|inferior|should be expelled|do not belong)\b",
    re.IGNORECASE,
)
_SENSITIVE_SYNTHETIC = re.compile(
    r"\b(?:killed|dead|death|murder|shooting|attack|airstrike|war|explosion|"
    r"earthquake|flood|wildfire|hospital|victim|missing)\b",
    re.IGNORECASE,
)


def evaluate_meta_policy(story: Story, image_path: Path | None = None) -> PolicyVerdict:
    """Return a conservative policy result without changing the story."""
    text = " ".join(filter(None, (story.title, story.raw_summary, story.post_content or "")))
    categories: list[str] = []
    reasons: list[str] = []
    block_categories: list[str] = []

    for category, pattern, reason in _UNAMBIGUOUS_BLOCKS:
        if re.search(pattern, text, re.IGNORECASE):
            categories.append(category)
            reasons.append(reason)
            block_categories.append(category)

    if _HATE_TARGET.search(text) and _HATE_ATTACK.search(text):
        categories.append("hateful_conduct")
        reasons.append("Potential degrading attack against a protected group.")
        block_categories.append("hateful_conduct")

    for category, pattern, reason in _REVIEW_SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            categories.append(category)
            reasons.append(reason)

    # Reporting or condemning prohibited speech is materially different from
    # endorsing it. Preserve the story for review instead of blocking it.
    if block_categories and _NEWS_CONTEXT.search(text):
        block_categories.clear()
        reasons.append("News-reporting context detected; human review required.")

    provenance = getattr(story, "image_provenance", "")
    if image_path is not None:
        if provenance not in APPROVED_IMAGE_PROVENANCE:
            categories.append("image_rights")
            reasons.append("Image provenance or reuse rights are not recorded.")
        if getattr(story, "image_is_synthetic", False) and _SENSITIVE_SYNTHETIC.search(text):
            categories.append("synthetic_sensitive_event")
            reasons.append("Synthetic image depicts or accompanies a sensitive real-world event.")

    categories = list(dict.fromkeys(categories))
    reasons = list(dict.fromkeys(reasons))
    if block_categories:
        decision = PolicyDecision.BLOCK
    elif categories:
        decision = PolicyDecision.REVIEW
    else:
        decision = PolicyDecision.PASS
    return PolicyVerdict(decision, tuple(categories), tuple(reasons))


def apply_policy_verdict(story: Story, verdict: PolicyVerdict) -> Story:
    story.policy_decision = verdict.decision.value
    story.policy_categories = list(verdict.categories)
    story.policy_reasons = list(verdict.reasons)
    story.policy_version = verdict.policy_version
    return story


def record_policy_decision(story: Story, verdict: PolicyVerdict, action: str) -> None:
    """Append a structured policy decision to the existing posting audit."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": RUN_ID,
        "title": story.title[:80],
        "source_url": story.source_url,
        "decision": f"POLICY_{verdict.decision.value}",
        "action": action,
        "categories": list(verdict.categories),
        "reason": verdict.reason[:500],
        "policy_version": verdict.policy_version,
    }
    try:
        with Path(config.POSTING_DECISIONS_PATH).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.error("Could not write Meta policy decision: %s", exc)
