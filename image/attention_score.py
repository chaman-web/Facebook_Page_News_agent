"""
image/attention_score.py — Pre-publish Human Attention Score

Scores a composed news card on 7 editorial criteria using the local Ollama LLM.
This is an internal quality-control gate, NOT Facebook's algorithm score.

Three improvements over naive LLM scoring:
  1. VISUAL CONTEXT INJECTION — pixel-level card analysis fed into the prompt so
     the text-only LLM scores against real card data, not guesses.
  2. CHAIN-OF-THOUGHT REASONING — LLM must justify each score with one specific
     observation before writing the number. Prevents lazy score clustering.
  3. SCORE LOG — every result written to score_log.jsonl for future correlation
     with Facebook Insights (reach, engagement) in Phase 7.

Score breakdown:
  Headline clarity       /20
  Visual impact          /20
  Story importance       /20
  Mobile readability     /15
  Emotional interest     /10
  Brand consistency      /10
  Professional quality    /5
  TOTAL                  /100

Verdict thresholds:
  80–100 → PUBLISH
  70–79  → PUBLISH if story priority is high (Tier 1 / breaking)
  55–69  → IMPROVE (skip — image needs work)
  <55    → REGENERATE (reject entirely)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import requests

import config
from models import Story

logger = logging.getLogger(__name__)

# ── Score thresholds ──────────────────────────────────────────────────────────
THRESHOLD_PUBLISH         = 80   # 80+ → always publish
THRESHOLD_PUBLISH_IF_HIGH = 70   # 70–79 → publish if high-priority story
THRESHOLD_IMPROVE         = 55   # 55–69 → skip (image needs work)
# < 55 → REGENERATE (reject entirely)

SCORE_LOG_PATH = Path("score_log.jsonl")


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class AttentionResult:
    headline_clarity:     int
    visual_impact:        int
    story_importance:     int
    mobile_readability:   int
    emotional_interest:   int
    brand_consistency:    int
    professional_quality: int
    total:                int
    verdict:              str
    reasoning:            dict  = field(default_factory=dict)
    notes:                str   = ""
    raw_response:         str   = ""
    scored_by:            str   = "llm"

    def scorecard(self) -> str:
        def bar(val, max_val, width=20):
            filled = int(val / max_val * width)
            return "█" * filled + "░" * (width - filled)

        lines = [
            "",
            f"  ┌─ ATTENTION SCORE ({self.scored_by.upper()}) {'─' * 28}┐",
            f"  │  Headline clarity       {self.headline_clarity:>3}/20  {bar(self.headline_clarity,20)}  │",
            f"  │  Visual impact          {self.visual_impact:>3}/20  {bar(self.visual_impact,20)}  │",
            f"  │  Story importance       {self.story_importance:>3}/20  {bar(self.story_importance,20)}  │",
            f"  │  Mobile readability     {self.mobile_readability:>3}/15  {bar(self.mobile_readability,15)}  │",
            f"  │  Emotional interest     {self.emotional_interest:>3}/10  {bar(self.emotional_interest,10)}  │",
            f"  │  Brand consistency      {self.brand_consistency:>3}/10  {bar(self.brand_consistency,10)}  │",
            f"  │  Professional quality   {self.professional_quality:>3}/5   {bar(self.professional_quality,5)}   │",
            f"  │  {'─' * 52}│",
            f"  │  TOTAL  {self.total:>3}/100   VERDICT → {self.verdict:<24}│",
            f"  └{'─' * 54}┘",
        ]
        if self.reasoning:
            lines.append("  Reasoning:")
            labels = {
                "headline_clarity":    "Headline clarity   ",
                "visual_impact":       "Visual impact      ",
                "story_importance":    "Story importance   ",
                "mobile_readability":  "Mobile readability ",
                "emotional_interest":  "Emotional interest ",
                "brand_consistency":   "Brand consistency  ",
                "professional_quality":"Professional quality",
            }
            for key, label in labels.items():
                r = self.reasoning.get(key, "")
                if r:
                    lines.append(f"    {label}: {r[:90]}")
        return "\n".join(lines)


# ── Public interface ──────────────────────────────────────────────────────────

def score_attention(
    story: Story,
    image_path: Optional[Path] = None,
    mobile_issues: Optional[dict] = None,
) -> AttentionResult:
    """
    Score the card. Writes result to score_log.jsonl.
    Falls back to heuristic if Ollama is unavailable.
    """
    card_facts = _extract_card_facts(image_path) if image_path else {}
    prompt     = _build_prompt(story, image_path, mobile_issues, card_facts)

    try:
        raw    = _call_ollama(prompt)
        result = _parse_response(raw, story)
        result.scored_by = "llm"
    except Exception as exc:
        logger.warning("Attention score LLM call failed (%s) — heuristic fallback", exc)
        result = _heuristic_score(story, mobile_issues, card_facts)

    logger.info("%s", result.scorecard())
    _write_score_log(story, result, image_path)
    return result


# ── Improvement 1: Visual context extraction ──────────────────────────────────

def _extract_card_facts(image_path: Path) -> dict:
    """
    Extract concrete measurable facts from the composed card pixels.
    Injected into the LLM prompt so it scores against real card data.
    """
    from PIL import Image as PILImage

    facts: dict = {}
    if not image_path or not image_path.exists():
        return facts

    try:
        arr = np.array(PILImage.open(image_path).convert("RGB"))
        h, w = arr.shape[:2]

        # Headline zone background brightness
        hl_zone = arr[:int(h * 0.28), :int(w * 0.55)]
        hl_mean = float(hl_zone.mean())
        facts["headline_bg"] = (
            f"{'DARK' if hl_mean < 80 else 'BRIGHT'} background behind headline "
            f"(mean={hl_mean:.0f}/255). "
            f"{'Good white-text contrast.' if hl_mean < 80 else 'Risk: white text hard to read.'}"
        )

        # Impact word red highlight detection
        r_ch, g_ch = hl_zone[:,:,0], hl_zone[:,:,1]
        red_pct = float(((r_ch.astype(int) - g_ch.astype(int) > 80) & (r_ch > 150)).sum()) \
                  / hl_zone.size * 3 * 100
        facts["impact_highlight"] = (
            f"Impact word highlight {'DETECTED' if red_pct > 0.3 else 'NOT FOUND'} "
            f"({red_pct:.1f}% red pixels in headline zone). "
            f"{'Adds urgency.' if red_pct > 0.3 else 'No visual emphasis on key word.'}"
        )

        # Photo sharpness via Laplacian variance
        mid  = arr[int(h*0.30):int(h*0.70), int(w*0.10):int(w*0.90)]
        gray = mid.mean(axis=2).astype(np.float32)
        lap  = (gray[1:-1,1:-1]*4 - gray[:-2,1:-1] - gray[2:,1:-1]
                - gray[1:-1,:-2] - gray[1:-1,2:])
        bv   = float(lap.var())
        sharpness = ("VERY SHARP" if bv > 300 else "SHARP" if bv > 150
                     else "ACCEPTABLE" if bv > 80 else "SOFT" if bv > 30 else "BLURRY")
        facts["photo_sharpness"] = (
            f"Photo sharpness: {sharpness} (Laplacian var={bv:.0f}). "
            f"{'Professional quality.' if bv > 80 else 'Blurry — weakens visual credibility.'}"
        )

        # Footer darkness
        footer_b = float(arr[h-200:, :].mean())
        facts["footer_darkness"] = (
            f"Footer mean brightness {footer_b:.0f}/255 — "
            f"{'DARK: logo/source readable.' if footer_b < 80 else 'TOO BRIGHT: logo may be invisible.'}"
        )

        # Center photo subject visibility
        center   = arr[int(h*0.35):int(h*0.65), int(w*0.20):int(w*0.80)]
        center_b = float(center.mean())
        center_s = float(center.std())
        facts["photo_center"] = (
            f"Photo center: brightness={center_b:.0f}/255, texture={center_s:.0f}. "
            + ("Subject visible." if center_b > 30 and center_s > 15
               else "Center appears flat or gradient-crushed.")
        )

        # Brand color — top border should be RED
        tb      = arr[:6, :]
        tb_r, tb_g, tb_b = float(tb[:,:,0].mean()), float(tb[:,:,1].mean()), float(tb[:,:,2].mean())
        red_ok  = tb_r > 150 and tb_g < 80 and tb_b < 80
        facts["brand_colors"] = (
            f"Top border R={tb_r:.0f} G={tb_g:.0f} B={tb_b:.0f} — "
            f"{'RED border confirmed ✓ brand consistent.' if red_ok else 'Unexpected border color — possible brand issue.'}"
        )

        # Photo brightness distribution
        pv       = arr[int(h*0.25):int(h*0.75), :].mean(axis=2).flatten()
        d_pct    = float((pv < 60).sum())  / len(pv) * 100
        m_pct    = float(((pv >= 60) & (pv < 180)).sum()) / len(pv) * 100
        b_pct    = float((pv >= 180).sum()) / len(pv) * 100
        facts["photo_distribution"] = (
            f"Photo: {d_pct:.0f}% dark / {m_pct:.0f}% midtone / {b_pct:.0f}% bright. "
            + ("Well-exposed." if m_pct > 40 else "Very dark or very bright — may reduce impact.")
        )

    except Exception as exc:
        logger.debug("Card fact extraction failed: %s", exc)

    return facts


# ── Improvement 2: Chain-of-thought prompt ────────────────────────────────────

def _build_prompt(
    story: Story,
    image_path: Optional[Path],
    mobile_issues: Optional[dict],
    card_facts: dict,
) -> str:
    headline   = story.title
    category   = (story.category or "news").upper()
    source     = story.source_name or "Unknown"
    summary    = (story.raw_summary or "")[:300]
    has_image  = image_path is not None and image_path.exists()
    word_count = len(headline.split())

    mobile_section = (
        "MOBILE CHECKS: The following automated pixel checks FAILED:\n"
        + "\n".join(f"  ✗ {k}: {v}" for k, v in mobile_issues.items())
        if mobile_issues
        else "MOBILE CHECKS: All automated pixel checks PASSED ✓"
    )

    facts_section = (
        "CARD PIXEL ANALYSIS (measured from actual composed image):\n"
        + "\n".join(f"  • {v}" for v in card_facts.values())
        if card_facts
        else "CARD PIXEL ANALYSIS: No image (text-only post)."
    )

    no_image_note = "" if has_image else "\n⚠ No image — deduct 8–12 from Visual Impact automatically."

    return f"""You are a senior social media editor at "Global Pulse News", a professional Facebook news page.

Score this Facebook news card BEFORE publishing. This is an INTERNAL quality gate.
Score it as a real human thumb-scrolling their phone feed would experience it.
Be HONEST and SPECIFIC. Do NOT inflate scores. 20/20 means near-perfect execution.

━━━ POST DETAILS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HEADLINE   : {headline}
WORD COUNT : {word_count} words
CATEGORY   : {category}
SOURCE     : {source}
SUMMARY    : {summary}
IMAGE      : {"Composed card image exists" if has_image else "NO IMAGE — text-only post"}{no_image_note}

━━━ AUTOMATED PIXEL CHECKS ━━━━━━━━━━━━━━━━━━━━━━━━━
{mobile_section}

━━━ VISUAL CARD DATA (real pixel measurements) ━━━━━
{facts_section}

━━━ HOW TO SCORE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For EACH criterion you MUST follow two steps:
  Step 1 — Write ONE specific observation about this post that directly justifies your score.
           Reference the pixel data above where relevant. Be specific, not generic.
  Step 2 — Write the numeric score.

This two-step chain-of-thought is required. Generic reasoning like "the headline is good"
is not acceptable. Cite specific evidence.

━━━ CRITERIA ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. headline_clarity (0–20)
   Sharp, specific, instantly scannable?
   Deduct 3+ if >10 words. Deduct for vague subjects or missing actors.

2. visual_impact (0–20)
   Would this stop a thumb scrolling at full speed on a phone?
   Use sharpness and brightness data above. Deduct for blurry/flat/no image.

3. story_importance (0–20)
   Does this matter to a broad global audience TODAY?
   Deduct for niche, local, or low-stakes stories.

4. mobile_readability (0–15)
   Readable in under 2 seconds on a phone screen?
   Use mobile check results and headline background brightness above.

5. emotional_interest (0–10)
   Does this create urgency, curiosity, or emotional pull?
   Deduct for dry, flat, purely informational framing.

6. brand_consistency (0–10)
   Unmistakably Global Pulse News? Navy + white + red, clean badge, source in footer?
   Use brand color data above as evidence.

7. professional_quality (0–5)
   Clean, polished, error-free layout?
   Deduct for any issues in the pixel or mobile analysis.

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY with valid JSON. No markdown. No text outside the JSON object.

{{
  "reasoning": {{
    "headline_clarity":     "<specific observation citing evidence> → score: N",
    "visual_impact":        "<specific observation citing evidence> → score: N",
    "story_importance":     "<specific observation citing evidence> → score: N",
    "mobile_readability":   "<specific observation citing evidence> → score: N",
    "emotional_interest":   "<specific observation citing evidence> → score: N",
    "brand_consistency":    "<specific observation citing evidence> → score: N",
    "professional_quality": "<specific observation citing evidence> → score: N"
  }},
  "headline_clarity":     <int 0-20>,
  "visual_impact":        <int 0-20>,
  "story_importance":     <int 0-20>,
  "mobile_readability":   <int 0-15>,
  "emotional_interest":   <int 0-10>,
  "brand_consistency":    <int 0-10>,
  "professional_quality": <int 0-5>,
  "notes": "<one sentence: main strength and main weakness of this card>"
}}
"""


# ── Ollama call ───────────────────────────────────────────────────────────────

def _call_ollama(prompt: str) -> str:
    base_url = (config.OLLAMA_BASE_URL or "http://localhost:11434/v1").rstrip("/")
    model    = config.OLLAMA_MODEL or "llama3.2"

    payload = {
        "model":    model,
        "messages": [{"role": "user", "content": prompt}],
        "stream":   False,
        "options":  {
            "temperature": 0.15,
            "top_p":       0.9,
            "num_predict": 900,
        },
    }

    r = requests.post(f"{base_url}/chat/completions", json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ── Response parser ───────────────────────────────────────────────────────────

def _parse_response(raw: str, story: Story) -> AttentionResult:
    text  = re.sub(r"```(?:json)?", "", raw).strip()
    text  = re.sub(r",\s*([}\]])", r"\1", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in LLM response: {raw[:200]}")

    data = json.loads(match.group())

    def clamp(val, lo, hi):
        try:
            return max(lo, min(hi, int(val)))
        except (TypeError, ValueError):
            return lo

    hc = clamp(data.get("headline_clarity",     0), 0, 20)
    vi = clamp(data.get("visual_impact",         0), 0, 20)
    si = clamp(data.get("story_importance",      0), 0, 20)
    mr = clamp(data.get("mobile_readability",    0), 0, 15)
    ei = clamp(data.get("emotional_interest",    0), 0, 10)
    bc = clamp(data.get("brand_consistency",     0), 0, 10)
    pq = clamp(data.get("professional_quality",  0), 0,  5)

    # Strip " → score: N" suffix from reasoning values
    reasoning = {
        k: re.sub(r"\s*→\s*score:\s*\d+\s*$", "", str(v)).strip()[:120]
        for k, v in data.get("reasoning", {}).items()
    }

    total = hc + vi + si + mr + ei + bc + pq
    return AttentionResult(
        headline_clarity     = hc,
        visual_impact        = vi,
        story_importance     = si,
        mobile_readability   = mr,
        emotional_interest   = ei,
        brand_consistency    = bc,
        professional_quality = pq,
        total                = total,
        verdict              = _verdict(total, story),
        reasoning            = reasoning,
        notes                = str(data.get("notes", ""))[:200],
        raw_response         = raw,
        scored_by            = "llm",
    )


# ── Improvement 3: Score log ──────────────────────────────────────────────────

def _write_score_log(story: Story, result: AttentionResult, image_path: Optional[Path]) -> None:
    """
    Append one JSONL record to score_log.jsonl.
    fb_post_id / fb_reach / fb_engagement are null placeholders —
    fill them later via the Facebook Insights API (Phase 7).
    """
    record = {
        "ts":                   datetime.now(timezone.utc).isoformat(),
        "title":                story.title,
        "category":             story.category or "news",
        "source":               story.source_name or "",
        "image_path":           str(image_path) if image_path else None,
        "scored_by":            result.scored_by,
        "headline_clarity":     result.headline_clarity,
        "visual_impact":        result.visual_impact,
        "story_importance":     result.story_importance,
        "mobile_readability":   result.mobile_readability,
        "emotional_interest":   result.emotional_interest,
        "brand_consistency":    result.brand_consistency,
        "professional_quality": result.professional_quality,
        "total":                result.total,
        "verdict":              result.verdict,
        "notes":                result.notes,
        # Phase 7 placeholders
        "fb_post_id":           None,
        "fb_reach":             None,
        "fb_engagement":        None,
    }
    try:
        with open(SCORE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Could not write score log: %s", exc)


# ── Heuristic fallback ────────────────────────────────────────────────────────

def _heuristic_score(
    story: Story,
    mobile_issues: Optional[dict],
    card_facts: Optional[dict] = None,
) -> AttentionResult:
    """Rule-based fallback when Ollama is unavailable. Uses card_facts where available."""
    headline    = story.title or ""
    words       = len(headline.split())
    category    = (story.category or "news").lower()
    issue_count = len(mobile_issues) if mobile_issues else 0
    facts       = card_facts or {}

    hc = 16 if words <= 6 else 14 if words <= 10 else 11 if words <= 14 else 8

    vi = 15
    if "photo_sharpness" in facts:
        vi = (18 if "VERY SHARP"  in facts["photo_sharpness"] else
              16 if "SHARP"       in facts["photo_sharpness"] else
              10 if "SOFT"        in facts["photo_sharpness"] else
               6 if "BLURRY"     in facts["photo_sharpness"] else 15)
    vi = max(6, vi - issue_count * 2)

    high_impact = {"breaking", "war", "politics", "crime", "world"}
    si = 16 if category in high_impact else 12

    mr = 13
    if "headline_bg" in facts and "TOO BRIGHT" in facts["headline_bg"]:
        mr -= 4
    mr = max(5, mr - issue_count * 2)

    emotional = {"breaking", "war", "crime", "trending", "entertainment"}
    ei = 7 if category in emotional else 5

    bc = 9 if "brand_colors" in facts and "confirmed" in facts["brand_colors"] else 7

    pq = max(2, 5 - issue_count)

    total   = hc + vi + si + mr + ei + bc + pq
    verdict = _verdict(total, story)

    reasoning = {
        "headline_clarity":    f"{words} words — {'concise' if words <= 10 else 'long for mobile'}",
        "visual_impact":       facts.get("photo_sharpness", "No image data"),
        "story_importance":    f"{category} — {'high-impact' if category in high_impact else 'moderate'}",
        "mobile_readability":  facts.get("headline_bg", f"{issue_count} mobile check failure(s)"),
        "emotional_interest":  f"{'Emotionally engaging' if category in emotional else 'Informational'} category",
        "brand_consistency":   facts.get("brand_colors", "Brand data unavailable"),
        "professional_quality":f"{issue_count} mobile check failure(s)",
    }

    logger.info("Attention score (heuristic): %d/100 → %s", total, verdict)
    return AttentionResult(
        headline_clarity     = hc,
        visual_impact        = vi,
        story_importance     = si,
        mobile_readability   = mr,
        emotional_interest   = ei,
        brand_consistency    = bc,
        professional_quality = pq,
        total                = total,
        verdict              = verdict,
        reasoning            = reasoning,
        notes                = "Heuristic fallback — Ollama unavailable.",
        raw_response         = "",
        scored_by            = "heuristic",
    )


# ── Verdict helper ────────────────────────────────────────────────────────────

def _verdict(total: int, story: Story) -> str:
    category = (story.category or "news").lower()
    tier     = getattr(story, "tier", None)
    is_high  = (
        category in {"breaking", "war", "politics", "world", "crime"}
        or str(tier) in {"1", "EditorialTier.TIER1", "Tier1"}
    )
    if total >= THRESHOLD_PUBLISH:
        return "PUBLISH"
    elif total >= THRESHOLD_PUBLISH_IF_HIGH:
        return "PUBLISH" if is_high else "PUBLISH_IF_HIGH"
    elif total >= THRESHOLD_IMPROVE:
        return "IMPROVE"
    else:
        return "REGENERATE"
