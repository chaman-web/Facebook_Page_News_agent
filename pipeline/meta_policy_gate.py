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

POLICY_VERSION = "2026-09-09"
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
    r"reporting|said|says|alleged|accused|charged|arrested|trial|statement|"
    r"investigat(?:e|es|ed|ing|ion)|condemn(?:s|ed)?|court|prosecutors?|witnesses?)\b",
    re.IGNORECASE,
)

_UNAMBIGUOUS_BLOCKS: tuple[tuple[str, str, str], ...] = (
    (
        "violence_incitement",
        r"\b(?:i|everyone|people|we|you|they)\s+"
        r"(?:will|should|must|need to|ought to|deserve to)\s+"
        r"(?:kill|shoot|attack|bomb|burn|execute|rape)\b",
        "Direct encouragement of violence.",
    ),
    (
        "violence_incitement",
        r"(?:^|:\s*)(?:go (?:and )?)?(?:kill|shoot|attack|bomb|burn|execute|rape)\s+"
        r"(?:all|every|the|those|him|her|them)\b",
        "Direct imperative encouraging violence.",
    ),
    (
        "self_harm_instruction",
        r"\b(?:how to|best way to|instructions? (?:to|for))\s+"
        r"(?:commit suicide|kill yourself|self[- ]harm|starve yourself|purge)\b",
        "Instructions encouraging suicide or self-harm.",
    ),
    (
        "self_harm_instruction",
        r"\b(?:suicide|self[- ]harm|starving yourself|purging)\s+"
        r"(?:is|would be)\s+(?:the answer|a solution|best)\b",
        "Content promoting suicide, self-harm, or an eating disorder.",
    ),
    (
        "child_sexual_exploitation",
        r"\b(?:buy|sell|share|download|trade)\b.{0,40}\b"
        r"(?:child sexual|underage sexual|minor sexual)\b",
        "Sexual exploitation involving minors.",
    ),
    (
        "fraud",
        r"\b(?:send|pay|deposit|transfer|invest)\b.{0,60}\b"
        r"(?:guaranteed returns?|double your money|free money|risk[- ]free profit)\b",
        "Fraudulent financial solicitation.",
    ),
    (
        "credential_theft",
        r"\b(?:send|enter|share|confirm)\b.{0,35}\b"
        r"(?:password|one[- ]time (?:password|code)|otp|verification code|bank details)\b",
        "Request for private credentials or financial access information.",
    ),
    (
        "regulated_goods",
        r"\b(?:buy|sell|order|ship|deliver|trade)\b.{0,55}\b"
        r"(?:cocaine|heroin|meth(?:amphetamine)?|fentanyl|firearms?|guns?|ammunition|explosives?)\b",
        "Transaction involving drugs, weapons, or other regulated goods.",
    ),
    (
        "human_exploitation",
        r"\b(?:buy|sell|traffic|recruit|transport)\b.{0,55}\b"
        r"(?:people|persons?|women|men|children|workers?)\b.{0,35}\b"
        r"(?:for sex|sexual services?|forced labo(?:u)?r|slavery)\b",
        "Possible facilitation of human trafficking or exploitation.",
    ),
    (
        "sexual_exploitation",
        r"\b(?:buy|sell|share|download|trade|send)\b.{0,45}\b"
        r"(?:rape video|intimate images?|nudes?|non[- ]consensual sexual content)\b",
        "Possible solicitation or distribution of sexual exploitation content.",
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
    (
        "sexual_content",
        r"\b(?:explicit sexual (?:content|images?)|pornograph(?:y|ic)|nude (?:photo|image)s?|"
        r"sexual assault|rape allegation)\b",
        "Sexual or exploitation-related material needs editorial review.",
    ),
    (
        "self_harm_content",
        r"\b(?:suicide|self[- ]harm|eating disorder|anorexia|bulimia)\b",
        "Suicide, self-harm, or eating-disorder coverage needs editorial review.",
    ),
    (
        "targeted_harassment",
        r"\b(?:humiliate|harass|dox|ruin)\b.{0,45}\b(?:him|her|them|this person)\b",
        "Possible targeted harassment or exposure of a private person.",
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


def _sentences(text: str) -> list[str]:
    """Keep policy context local so one attribution cannot soften another sentence."""
    return [part.strip() for part in re.split(r"(?:\r?\n)+|(?<=[.!?])\s+", text) if part.strip()]


def _contextual_rule_matches(text: str, pattern: str) -> tuple[bool, bool]:
    """Return (matched, has_unattributed_match) for a block-level rule."""
    matched = False
    has_unattributed_match = False
    for sentence in _sentences(text):
        if re.search(pattern, sentence, re.IGNORECASE):
            matched = True
            if not _NEWS_CONTEXT.search(sentence):
                has_unattributed_match = True
    return matched, has_unattributed_match


def evaluate_meta_policy(story: Story, image_path: Path | None = None) -> PolicyVerdict:
    """Return a conservative policy result without changing the story."""
    hashtag_text = re.sub(r"[_#-]+", " ", " ".join(story.hashtags or []))
    text = "\n".join(filter(None, (
        story.title,
        story.raw_summary,
        story.post_content or "",
        story.card_headline or "",
        story.card_description or "",
        hashtag_text,
    )))
    categories: list[str] = []
    reasons: list[str] = []
    block_categories: list[str] = []
    reporting_context_found = False

    for category, pattern, reason in _UNAMBIGUOUS_BLOCKS:
        matched, has_unattributed_match = _contextual_rule_matches(text, pattern)
        if matched:
            categories.append(category)
            reasons.append(reason)
            if has_unattributed_match:
                block_categories.append(category)
            else:
                reporting_context_found = True

    for sentence in _sentences(text):
        if _HATE_TARGET.search(sentence) and _HATE_ATTACK.search(sentence):
            categories.append("hateful_conduct")
            reasons.append("Potential degrading attack against a protected group.")
            if _NEWS_CONTEXT.search(sentence):
                reporting_context_found = True
            else:
                block_categories.append("hateful_conduct")

    for category, pattern, reason in _REVIEW_SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            categories.append(category)
            reasons.append(reason)

    # Reporting or condemning prohibited speech is preserved for review. The
    # exception is local to the matching sentence, never the whole post.
    if reporting_context_found:
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
