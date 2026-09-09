"""
image/maker.py — Step 2: Professional Facebook news post image generator.

Design: Full-bleed photo with gradient overlays, editorial style.
Canvas: 1200 × 1500 px (4:5 mobile-first)

Fallback chain:
  Article image → relevant Pexels image → story-aware branded fallback
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import config
from models import Story
from pipeline.story_topic import card_topic

logger = logging.getLogger(__name__)

# ── Canvas ───────────────────────────────────────────────────────────────────
IMAGE_WIDTH  = 1200
IMAGE_HEIGHT = 1500

# ── Brand palette — FIXED across all categories ───────────────────────────────
NAVY         = (8,   16,  40)
NAVY_LIGHT   = (18,  32,  72)
RED          = (210, 30,  45)
RED_DARK     = (140, 18,  28)
WHITE        = (255, 255, 255)
OFF_WHITE    = (220, 225, 235)
GOLD         = (255, 200, 60)

# Every card uses RED as its structural accent — never per-category.
BRAND_ACCENT = RED

# ── Layout zones ─────────────────────────────────────────────────────────────
PHOTO_BOT    = 1170

# ── Typography ───────────────────────────────────────────────────────────────
LABEL_SIZE      = 28
HEADLINE_SIZE   = 80
HEADLINE_MIN    = 58
CONTEXT_SIZE    = 32
DATE_SIZE       = 24
SOURCE_SIZE     = 22

# ── Margins ──────────────────────────────────────────────────────────────────
ML = 72
MR = 72
MT = 60

# ── Font dir ──────────────────────────────────────────────────────────────────
_FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"

def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        _FONT_DIR / name,
        Path("/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()

# ── Category labels ───────────────────────────────────────────────────────────
CATEGORY_LABELS = {
    "breaking":      "BREAKING NEWS",
    "technology":    "TECHNOLOGY",
    "business":      "BUSINESS",
    "politics":      "POLITICS",
    "science":       "SCIENCE & HEALTH",
    "sports":        "SPORTS",
    "trending":      "TRENDING",
    "entertainment": "ENTERTAINMENT",
    "jobs":          "JOBS & OPPORTUNITIES",
    "world":         "WORLD NEWS",
    "crime":         "CRIME & JUSTICE",
    "climate":       "CLIMATE & ENVIRONMENT",
    "war":           "WAR & CONFLICT",
    "wellness":      "HEALTH & WELLNESS",
    "news":          "NEWS",
}

# ── Category badge dot colors (small dot inside badge — only per-category color) ─
CATEGORY_DOT_COLORS = {
    "breaking":      (210, 30,  45),
    "technology":    (0,   140, 255),
    "business":      (0,   180, 120),
    "politics":      (180, 60,  200),
    "science":       (0,   190, 200),
    "sports":        (255, 140, 0),
    "trending":      (255, 60,  130),
    "entertainment": (255, 200, 0),
    "jobs":          (50,  200, 100),
    "world":         (30,  100, 220),
    "crime":         (180, 30,  30),
    "climate":       (30,  180, 80),
    "war":           (200, 80,  0),
    "wellness":      (0,   200, 180),
    "news":          (100, 100, 110),
}

# ── Impact word detection ─────────────────────────────────────────────────────
# Words that carry strong emotional or editorial weight.
# The highest-priority match in the headline gets a red highlight box.
# Ordered by descending impact — first match wins.
_IMPACT_WORDS: list[tuple[int, list[str]]] = [
    # Tier 1 — life/death/catastrophe
    (1, ["KILLED", "KILLS", "DEAD", "DIED", "DEATH", "DEATHS",
         "ASSASSINATED", "MASSACRE", "GENOCIDE", "EXECUTED",
         "BOMBING", "BOMBED", "EXPLOSION", "EXPLODES", "EXPLODED",
         "CRASH", "CRASHED", "COLLAPSE", "COLLAPSED",
         "DISASTER", "CATASTROPHE", "DEVASTATING", "DESTROYED"]),
    # Tier 2 — conflict/war/crisis
    (2, ["WAR", "ATTACK", "ATTACKED", "STRIKE", "STRIKES", "STRUCK",
         "FIRES", "FIRED", "SHOOTS", "SHOT", "LAUNCHES", "LAUNCHED",
         "INVASION", "INVADED", "SIEGE", "CONFLICT", "BATTLE",
         "CRISIS", "EMERGENCY", "ALERT", "THREAT", "THREATENED",
         "SANCTIONS", "COUP", "ARRESTED", "JAILED", "CONVICTED"]),
    # Tier 3 — major political/economic events
    (3, ["BREAKING", "HISTORIC", "LANDMARK", "UNPRECEDENTED",
         "BREAKTHROUGH", "VICTORY", "DEFEATED", "RESIGNS", "RESIGNED", "FIRED",
         "BANNED", "SUSPENDED", "COLLAPSED", "BANKRUPT", "RECESSION",
         "EARTHQUAKE", "HURRICANE", "FLOOD", "WILDFIRE", "EPIDEMIC"]),
    # Tier 4 — strong verbs / superlatives
    (4, ["RECORD", "LARGEST", "BIGGEST", "FIRST", "LAST", "ONLY",
         "SHOCKING", "MAJOR", "CRITICAL", "URGENT", "SIGNIFICANT",
         "RISING", "SURGES", "SURGED", "PLUNGES", "PLUNGED",
         "WARNS", "WARNING", "FEARS", "DEMANDS", "REFUSES"]),
]

def _find_impact_words(headline: str) -> list[str]:
    """
    Find up to 2 most impactful words in the headline.
    Returns words as they appear in the headline (preserving case).
    Tier 1 words get priority — if 2 Tier 1 words found, return both.
    Otherwise return best from Tier 1 + best from Tier 2, etc.
    """
    words       = re.findall(r"[A-Za-z']+", headline)
    upper_words = [w.upper() for w in words]
    found: list[str] = []

    for _tier, word_list in _IMPACT_WORDS:
        for impact in word_list:
            if impact in upper_words:
                idx = upper_words.index(impact)
                original = words[idx]
                if original not in found:
                    found.append(original)
            if len(found) >= 2:
                return found
    return found


# Keep old name as alias for any legacy callers
def _find_impact_word(headline: str) -> Optional[str]:
    result = _find_impact_words(headline)
    return result[0] if result else None
DATE_SIZE       = 24
SOURCE_SIZE     = 22

# ── Margins ──────────────────────────────────────────────────────────────────
ML = 72    # margin left
MR = 72    # margin right
MT = 60    # margin top

# ── Font dir ─────────────────────────────────────────────────────────────────
_FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"

def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        _FONT_DIR / name,
        Path("/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()

# ── Category labels ───────────────────────────────────────────────────────────
CATEGORY_LABELS = {
    "breaking":      "BREAKING NEWS",
    "technology":    "TECHNOLOGY",
    "business":      "BUSINESS",
    "politics":      "POLITICS",
    "science":       "SCIENCE & HEALTH",
    "sports":        "SPORTS",
    "trending":      "TRENDING",
    "entertainment": "ENTERTAINMENT",
    "jobs":          "JOBS & OPPORTUNITIES",
    "world":         "WORLD NEWS",
    "crime":         "CRIME & JUSTICE",
    "climate":       "CLIMATE & ENVIRONMENT",
    "war":           "WAR & CONFLICT",
    "wellness":      "HEALTH & WELLNESS",
    # fallback — shown when category slug is unknown / unrecognised
    "news":          "NEWS",
}

# ── Brand accent — FIXED across all categories ───────────────────────────────
# Every card uses RED as its structural accent color.
# This is what makes Global Pulse News cards immediately recognizable.
BRAND_ACCENT = RED

# ── Category dot colors — used ONLY inside the badge (12px dot) ──────────────
# This is the sole per-category color on the entire card.
# It appears nowhere else — not on borders, rules, overlays, or the footer.
CATEGORY_DOT_COLORS = {
    "breaking":      (210, 30,  45),   # red
    "technology":    (0,   140, 255),  # blue
    "business":      (0,   180, 120),  # green
    "politics":      (180, 60,  200),  # purple
    "science":       (0,   190, 200),  # teal
    "sports":        (255, 140, 0),    # orange
    "trending":      (255, 60,  130),  # pink
    "entertainment": (255, 200, 0),    # gold
    "jobs":          (50,  200, 100),  # emerald green
    "world":         (30,  100, 220),  # royal blue
    "crime":         (180, 30,  30),   # dark red
    "climate":       (30,  180, 80),   # forest green
    "war":           (200, 80,  0),    # burnt orange
    "wellness":      (0,   200, 180),  # mint
    "news":          (100, 100, 110),  # neutral grey
}


# ── Mobile visibility thresholds ─────────────────────────────────────────────
# All checks simulate a 390px-wide phone screen (iPhone 14 viewport).
# The 1200px card scales to ~390px wide on a phone — scale factor ~0.325.
# Sizes below are in full-resolution (1200px) space unless noted.

# Minimum font size that reads on phone after downscale (58px × 0.325 ≈ 19px rendered)
MOBILE_MIN_HEADLINE_PX = 58

# Headline area: badge top-line to rule. Must not exceed this height or text is too dense.
MOBILE_MAX_HEADLINE_HEIGHT = 340   # px on full-res canvas

# Max words in headline for 1–2 second readability at phone size
MOBILE_MAX_HEADLINE_WORDS = 12

# Minimum RMS contrast between headline text region and its background
# Measured as mean absolute difference of pixel brightness in overlay zone
MOBILE_MIN_CONTRAST = 55   # 0–255 scale

# Footer zone: bottom 200px must not be too bright (source/logo must be readable)
MOBILE_FOOTER_MIN_DARKNESS = 30   # max mean brightness allowed in footer zone

# Maximum fraction of canvas covered by text+overlay regions (avoid clutter)
MOBILE_MAX_TEXT_COVERAGE = 0.55


# ── Public interface ──────────────────────────────────────────────────────────

def create_news_image(story: Story) -> Optional[Path]:
    """
    Fetch photo, compose card, run mobile visibility checks.

    Redesign strategy on failure:
      Attempt 1 — normal composition with fetched photo.
      Attempt 2 — relevant stock or generated photo.
      Attempt 3 — broader story keywords.
      Attempt 4 — category keywords.
      If all attempts fail → create a unique story-aware branded card.
    """
    IMAGES_DIR = config.IMAGES_DIR
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    cat = getattr(story, "category", "breaking")
    _CATEGORY_FALLBACK: dict[str, str] = {
        "breaking": "news dark", "war": "conflict dramatic",
        "world": "world global", "politics": "parliament government",
        "crime": "police justice", "technology": "technology digital",
        "business": "business finance", "science": "science laboratory",
        "sports": "sports stadium", "entertainment": "theatre stage",
        "climate": "nature environment", "wellness": "health medical",
        "jobs": "office career", "trending": "crowd people",
    }
    fallback_kw = _CATEGORY_FALLBACK.get(cat, "news")

    # Three search strategies — each tries a different keyword to get a different photo.
    # Fetched lazily so we only call Pexels when the previous attempt failed.
    search_strategies = []
    if getattr(story, "article_image_url", None):
        search_strategies.append(lambda: _fetch_article_photo(story))
    search_strategies.extend([
        lambda: _fetch_photo(story),
        lambda: _fetch_photo_by_keyword(_keywords(story.title, broad=True), page=2),
        lambda: _fetch_photo_by_keyword(fallback_kw, page=1),
    ])

    fail_summary = ""
    for attempt, fetch_fn in enumerate(search_strategies):
        current_photo = fetch_fn()
        if current_photo is None:
            logger.warning("Image fetch strategy %d returned nothing — skipping.", attempt + 1)
            continue

        image  = _compose(current_photo, story, strong_gradients=False)
        issues = _mobile_visibility_check(image, story, raw_photo=current_photo)

        if not issues:
            safe     = "".join(c if c.isalnum() or c in "-_" else "_" for c in story.title[:45])
            out_path = IMAGES_DIR / f"{safe}.jpg"
            image.save(out_path, "JPEG", quality=93, optimize=True)
            logger.info("✅ Image saved (attempt %d, strategy %d): %s", attempt + 1, attempt + 1, out_path)
            return out_path

        fail_summary = "; ".join(f"{k}: {v}" for k, v in issues.items())
        logger.warning(
            "Image attempt %d failed mobile check — fetching different photo. Issues: %s",
            attempt + 1, fail_summary,
        )

    # No suitable photo: return None so the caller retains the story as a draft.
    logger.warning(
        "All external image attempts failed for '%s' — using branded fallback. Last issues: %s",
        story.title[:60], fail_summary,
    )
    return create_fallback_card(story)


def create_fallback_card(story: Story) -> Path:
    """Create a guaranteed, story-aware card when no external image is available."""
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    background = _fallback_background(story)
    card = _compose(background, story, strong_gradients=False)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in story.title[:45])
    out_path = config.IMAGES_DIR / f"{safe}_fallback.jpg"
    card.save(out_path, "JPEG", quality=93, optimize=True)
    logger.info("✅ Story-aware fallback image saved: %s", out_path)
    return out_path


def _fallback_background(story: Story) -> Image.Image:
    """Return a deterministic abstract background unique to the story and topic."""
    topic = card_topic(story)
    color = CATEGORY_DOT_COLORS.get(topic, CATEGORY_DOT_COLORS["news"])
    identity = f"{story.source_url}|{story.title}".encode("utf-8", errors="ignore")
    seed = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")
    rng = random.Random(seed)
    variant = seed % 6
    variant_colors = (GOLD, (0, 170, 255), RED, (0, 205, 155), (185, 80, 220), (255, 125, 45))
    variant_color = variant_colors[variant]
    visual_color = tuple(
        int(channel * 0.58 + variant_channel * 0.42)
        for channel, variant_channel in zip(color, variant_color)
    )

    background = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), NAVY)
    draw = ImageDraw.Draw(background, "RGBA")
    for y in range(IMAGE_HEIGHT):
        t = y / max(IMAGE_HEIGHT - 1, 1)
        wave = 0.5 + 0.5 * ((y + (seed % 420)) % 420) / 420
        strength = 0.08 + (0.18 * t) + (0.04 * wave)
        draw.line(
            (0, y, IMAGE_WIDTH, y),
            fill=(
                min(255, int(NAVY[0] + visual_color[0] * strength)),
                min(255, int(NAVY[1] + visual_color[1] * strength)),
                min(255, int(NAVY[2] + visual_color[2] * strength)),
                255,
            ),
        )

    # Story-seeded glows and guide lines ensure consecutive fallbacks never
    # look like copies, even when they share a category.
    for _ in range(4):
        cx = rng.randint(180, IMAGE_WIDTH + 120)
        cy = rng.randint(430, 1120)
        radius = rng.randint(150, 390)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(*visual_color, rng.randint(10, 24)),
            outline=(*WHITE, rng.randint(12, 28)),
            width=rng.randint(2, 5),
        )
    slope = -1 if variant % 2 else 1
    for offset in range(-650, 950, 135 + (variant * 7)):
        x2 = offset + slope * (760 + variant * 35)
        draw.line((offset, IMAGE_HEIGHT, x2, 260), fill=(*WHITE, 13), width=3)

    _draw_fallback_motif(draw, topic, visual_color, rng, variant)
    return background


def _draw_fallback_motif(
    draw: ImageDraw.ImageDraw,
    topic: str,
    color: tuple[int, int, int],
    rng: random.Random,
    variant: int,
) -> None:
    """Draw a factual, non-photographic topic cue in the card's open center."""
    shift = (variant - 2) * 24
    cx, cy = 780 + shift, 760 + rng.randint(-45, 45)
    bright = (*color, 150)
    soft = (*color, 70)
    white = (*WHITE, 95)

    if topic == "business":
        base = cy + 250
        heights = [180 + rng.randint(0, 80), 290 + rng.randint(0, 80), 410 + rng.randint(0, 80)]
        for index, height in enumerate(heights):
            x = cx - 290 + index * 190
            draw.rounded_rectangle((x, base - height, x + 105, base), radius=18, fill=soft, outline=bright, width=5)
        points = [(cx - 330, base - 90), (cx - 90, base - 270), (cx + 120, base - 220), (cx + 330, base - 470)]
        draw.line(points, fill=white, width=12, joint="curve")
        draw.polygon(((cx + 330, base - 470), (cx + 275, base - 448), (cx + 315, base - 405)), fill=white)
    elif topic == "politics":
        draw.polygon(((cx - 360, cy - 110), (cx, cy - 360), (cx + 360, cy - 110)), fill=soft, outline=bright)
        draw.rectangle((cx - 375, cy - 105, cx + 375, cy - 55), fill=bright)
        for x in range(cx - 300, cx + 301, 150):
            draw.rounded_rectangle((x, cy - 40, x + 72, cy + 330), radius=12, fill=soft, outline=white, width=4)
        draw.rectangle((cx - 400, cy + 335, cx + 400, cy + 390), fill=bright)
    elif topic in {"technology", "science", "wellness"}:
        nodes = [(cx + rng.randint(-340, 340), cy + rng.randint(-300, 300)) for _ in range(9)]
        for left, right in zip(nodes, nodes[1:]):
            draw.line((left, right), fill=soft, width=6)
        for x, y in nodes:
            radius = rng.randint(18, 34)
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=bright, outline=white, width=4)
        if topic == "wellness":
            draw.line((cx-390, cy+60, cx-170, cy+60, cx-80, cy-110, cx+30, cy+190, cx+135, cy, cx+390, cy), fill=white, width=12)
    elif topic in {"climate", "world"}:
        radius = 330
        draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), outline=bright, width=10)
        for inset in (95, 205):
            draw.ellipse((cx-radius+inset, cy-radius, cx+radius-inset, cy+radius), outline=soft, width=5)
        for dy in (-155, 0, 155):
            draw.arc((cx-radius, cy-radius+dy, cx+radius, cy+radius-dy), 0, 180, fill=white, width=5)
    elif topic in {"crime", "war"}:
        shield = ((cx, cy-360), (cx+300, cy-220), (cx+245, cy+145), (cx, cy+390), (cx-245, cy+145), (cx-300, cy-220))
        draw.polygon(shield, fill=soft, outline=bright)
        draw.line((cx, cy-260, cx, cy+255), fill=white, width=10)
        draw.line((cx-185, cy-20, cx+185, cy-20), fill=white, width=10)
    elif topic == "sports":
        for inset in (0, 70, 140):
            draw.arc((cx-390+inset, cy-260+inset, cx+390-inset, cy+420-inset), 190, 350, fill=bright, width=14)
        draw.ellipse((cx-105, cy-105, cx+105, cy+105), outline=white, width=10)
    elif topic == "entertainment":
        draw.polygon(((cx-410, cy+360), (cx-220, cy-360), (cx-30, cy+360)), fill=soft)
        draw.polygon(((cx+20, cy+360), (cx+220, cy-360), (cx+420, cy+360)), fill=soft)
        draw.ellipse((cx-90, cy-90, cx+90, cy+90), fill=bright, outline=white, width=6)
    elif topic == "jobs":
        draw.rounded_rectangle((cx-360, cy-180, cx+360, cy+300), radius=42, fill=soft, outline=bright, width=8)
        draw.arc((cx-150, cy-350, cx+150, cy-70), 180, 360, fill=white, width=12)
        draw.line((cx-360, cy+20, cx+360, cy+20), fill=white, width=8)
    else:
        # Breaking/general news: a broadcast pulse with story-seeded geometry.
        for radius in (105, 205, 315):
            draw.arc((cx-radius, cy-radius, cx+radius, cy+radius), 205+variant*5, 515-variant*4, fill=bright, width=12)
        draw.ellipse((cx-42, cy-42, cx+42, cy+42), fill=white)


def _mobile_visibility_check(image: Image.Image, story: Story,
                              raw_photo: Optional[Image.Image] = None) -> dict[str, str]:
    """
    Simulate how the card looks on a phone screen and return a dict of failures.
    Returns {} if all checks pass. Each failing key maps to a human-readable reason.

    raw_photo: the original photo before composition (used for blur + bright-patch
               checks that must run on background pixels, not on rendered text).

    Checks:
      headline_length        — too many words (can't read in 1–2 sec)
      headline_contrast      — headline region background too bright
      subject_covered        — center photo gradient-crushed
      footer_darkness        — footer too bright → source/logo unreadable
      text_coverage          — gradient overkill on top half
      logo_zone              — logo background too bright
      source_zone            — source text background too bright
      headline_bright_patch  — bright photo patch behind headline (raw photo)
      photo_blur             — blurry/soft photo (raw photo Laplacian)
    """
    import numpy as np

    issues: dict[str, str] = {}
    w, h   = image.size   # 1200 × 1500

    # Convert to numpy for pixel analysis
    arr = np.array(image)   # shape (H, W, 3), dtype uint8

    # ── CHECK 1: Headline font size ───────────────────────────────────────────
    # We can't re-measure font size from pixels, but we CAN measure the
    # headline region height. If it's taller than the allowed max, text is too
    # small (forced to shrink) → flag it.
    # Headline sits from badge bottom (~MT + badge_h ~46) down ~340px.
    hl_region_h = MOBILE_MAX_HEADLINE_HEIGHT
    # Proxy: measure brightness variance in top-left text zone.
    # A very short headline in huge font = high variance (white on dark = big swings).
    # Too many lines in tiny font = lower variance per line.
    # This is a heuristic; the reliable size check is word count (check 2).

    # ── CHECK 2: Headline word count (1–2 second readability) ────────────────
    headline_words = len(_short_headline(story.title).split())
    if headline_words > MOBILE_MAX_HEADLINE_WORDS:
        issues["headline_length"] = (
            f"{headline_words} words > {MOBILE_MAX_HEADLINE_WORDS} max "
            f"(too slow to scan on phone)"
        )

    # ── CHECK 3: Headline contrast — text zone vs background ─────────────────
    # Headline sits in top overlay zone: rows MT..MT+MOBILE_MAX_HEADLINE_HEIGHT
    hl_zone = arr[MT : MT + MOBILE_MAX_HEADLINE_HEIGHT, ML : w - MR]
    if hl_zone.size > 0:
        brightness = hl_zone.mean(axis=2)
        mean_dark  = brightness.mean()
        std_bright = brightness.std()
        if mean_dark > 160:
            issues["headline_contrast"] = (
                f"Headline background too bright (mean={mean_dark:.0f}/255). "
                f"White text will be invisible on phone."
            )
        elif std_bright < MOBILE_MIN_CONTRAST:
            issues["headline_contrast"] = (
                f"Headline zone lacks contrast (std={std_bright:.0f} < {MOBILE_MIN_CONTRAST}). "
                f"Text may wash out."
            )

    # ── CHECK 4: Subject occlusion — center photo zone ────────────────────────
    # The visual subject of news photos is typically center-frame.
    # Only flag if the center zone is extremely dark AND has very low variance
    # (meaning it's gradient-crushed, not a legitimately dark photo).
    center_y1 = int(h * 0.35)
    center_y2 = int(h * 0.65)
    center_x1 = int(w * 0.20)
    center_x2 = int(w * 0.80)
    center_zone = arr[center_y1:center_y2, center_x1:center_x2]
    if center_zone.size > 0:
        center_brightness = center_zone.mean()
        center_variance   = center_zone.std()
        # Crushed = very dark AND very uniform (no photo texture visible)
        if center_brightness < 20 and center_variance < 8:
            issues["subject_covered"] = (
                f"Center of photo is pitch black (mean={center_brightness:.0f}, "
                f"std={center_variance:.0f}). Gradient is crushing the subject."
            )

    # ── CHECK 5: Footer darkness (logo + source must be readable) ─────────────
    # Footer zone: bottom 200px. We want it DARK so white text shows.
    footer_zone = arr[h - 200 :, :]
    if footer_zone.size > 0:
        footer_brightness = footer_zone.mean()
        if footer_brightness > 120:
            issues["footer_darkness"] = (
                f"Footer too bright (mean={footer_brightness:.0f}). "
                f"Logo and source name won't be readable."
            )

    # ── CHECK 6: Text coverage — gradient overkill (not dark-photo false positive) ─
    # Only flag if top half is very dark AND center zone is dark AND low-variance.
    # Low variance means the gradients crushed the photo texture, not that the
    # photo is legitimately dark (war, night, low-light — these have texture).
    top_half = arr[: h // 2, :]
    if top_half.size > 0:
        dark_pixel_count  = (top_half.mean(axis=2) < 40).sum()
        total_top_pixels  = (h // 2) * w
        dark_fraction     = dark_pixel_count / total_top_pixels
        center_crushed    = (
            center_zone.size > 0
            and center_zone.mean() < 40
            and center_zone.std() < 8    # <8 = truly solid/degenerate, not a dark real photo
        )
        if dark_fraction > MOBILE_MAX_TEXT_COVERAGE and center_crushed:
            issues["text_coverage"] = (
                f"Top half {dark_fraction:.0%} dark-covered > {MOBILE_MAX_TEXT_COVERAGE:.0%} "
                f"and center crushed (mean={center_zone.mean():.0f}, std={center_zone.std():.0f}). "
                f"Gradient overkill."
            )

    # ── CHECK 7: Logo zone brightness ────────────────────────────────────────
    # Logo sits bottom-left: rows (h-200)..(h-90), cols ML..(ML+120)
    logo_zone = arr[h - 200 : h - 85, ML : ML + 120]
    if logo_zone.size > 0:
        logo_brightness = logo_zone.mean()
        if logo_brightness > 140:
            issues["logo_zone"] = (
                f"Logo area too bright (mean={logo_brightness:.0f}). "
                f"Logo may be invisible."
            )

    # ── CHECK 8: Source text zone brightness ──────────────────────────────────
    # Source name sits bottom-center: rows (h-160)..(h-60), cols (ML+140)..(w-MR-200)
    src_zone = arr[h - 160 : h - 55, ML + 140 : w - MR - 200]
    if src_zone.size > 0:
        src_brightness = src_zone.mean()
        if src_brightness > 130:
            issues["source_zone"] = (
                f"Source text area too bright (mean={src_brightness:.0f}). "
                f"Source name won't be readable."
            )

    # ── CHECK 9: Bright patch under headline text ─────────────────────────────
    # Must run on raw photo (before text rendering). White headline text pixels
    # make p99=255 on the composed image, making this check useless there.
    # We simulate the top gradient attenuation on the raw photo, then check
    # if bright patches still bleed through in the headline zone.
    if raw_photo is not None:
        rp     = _smart_crop(raw_photo.convert("RGB"), w, h)
        rp_arr = np.array(rp).astype(np.float32)
        fade_end = int(h * 0.42)
        hl_end   = MT + MOBILE_MAX_HEADLINE_HEIGHT
        for y in range(min(fade_end, hl_end)):
            t     = 1.0 - y / fade_end
            alpha = (230 * t) / 255
            rp_arr[y] = rp_arr[y] * (1.0 - alpha) + np.array(NAVY, dtype=np.float32) * alpha
        hl_zone_raw = rp_arr[MT + 56 : hl_end, ML : int(w * 0.55)]
        if hl_zone_raw.size > 0:
            p99_raw = float(np.percentile(hl_zone_raw.mean(axis=2), 99))
            if p99_raw > 160:
                issues["headline_bright_patch"] = (
                    f"Bright patch behind headline after overlay (p99={p99_raw:.0f}/255). "
                    f"White text may be unreadable on phone."
                )

    # ── CHECK 10: Photo sharpness (blur detection via Laplacian variance) ──────
    # Run on raw photo — gradient overlay kills texture in the composed image.
    # Laplacian variance: sharp → high, blurry/solid → near zero.
    # Threshold 80 calibrated: blurry <10, soft ~30, sharp real photo >150.
    photo_src = raw_photo if raw_photo is not None else image
    pf_arr    = np.array(_smart_crop(photo_src.convert("RGB"), w, h))
    mid_y1    = int(h * 0.30)
    mid_y2    = int(h * 0.70)
    center_crop = pf_arr[mid_y1:mid_y2, int(w * 0.10) : int(w * 0.90)]
    if center_crop.size > 0:
        gray = center_crop.mean(axis=2).astype(np.float32)
        lap  = (
            gray[1:-1, 1:-1] * 4
            - gray[:-2, 1:-1]
            - gray[2:,  1:-1]
            - gray[1:-1, :-2]
            - gray[1:-1, 2:]
        )
        blur_score     = float(lap.var())
        BLUR_THRESHOLD = 80.0
        if blur_score < BLUR_THRESHOLD:
            issues["photo_blur"] = (
                f"Photo appears blurry or soft (Laplacian var={blur_score:.0f} "
                f"< {BLUR_THRESHOLD:.0f}). Will look low-quality on phone."
            )

    # ── CHECK 11: Facebook safe zone — headline and footer edge clearance ────────
    # Facebook can crop or overlay UI chrome on the top/bottom ~50px on some devices.
    # Headline badge starts at MT (y=60) — safe.
    # Footer bottom edge is IMAGE_HEIGHT − 6px border — safe.
    # What can fail: if the headline somehow starts above y=50, or the footer
    # content (source, date) is within 40px of the absolute bottom.
    SAFE_TOP    = 50    # headline must start at or below this y
    SAFE_BOTTOM = 40    # footer content must end at or above (IMAGE_HEIGHT - SAFE_BOTTOM)

    if MT < SAFE_TOP:
        issues["safe_zone_top"] = (
            f"Badge/headline starts at y={MT}, below safe top margin of {SAFE_TOP}px. "
            f"May be cropped by Facebook UI chrome."
        )

    # Footer mid is IMAGE_HEIGHT - 95; content extends ~50px either side.
    footer_content_bottom = IMAGE_HEIGHT - 95 + 50   # ≈ IMAGE_HEIGHT - 45
    if footer_content_bottom > IMAGE_HEIGHT - SAFE_BOTTOM:
        issues["safe_zone_bottom"] = (
            f"Footer content reaches y={footer_content_bottom}, within {SAFE_BOTTOM}px "
            f"of bottom edge. May be cropped on some devices."
        )

    if issues:
        logger.debug("Mobile check failures: %s", issues)
    else:
        logger.debug("Mobile visibility check passed ✓")

    return issues

def _fetch_photo_by_keyword(keyword: str, page: int = 1) -> Optional[Image.Image]:
    """Fetch a photo directly by keyword and page offset — guarantees a different result each call."""
    if config.PEXELS_API_KEY:
        photo = _pexels(keyword, page=page)
        if photo:
            return photo
    return None


def _fetch_article_photo(story: Story) -> Optional[Image.Image]:
    """Prefer the publisher's own story image when it is usable."""
    url = getattr(story, "article_image_url", None)
    if not url:
        return None
    logger.info("Trying article image from %s", story.source_name)
    image = _download(url)
    if image:
        logger.info("Using authentic article image from %s", story.source_name)
    return image


def _relevance_terms(text: str) -> set[str]:
    stop = {
        "about", "after", "before", "from", "have", "image", "news", "photo",
        "says", "that", "their", "this", "with", "world", "people", "person",
    }
    return {
        word.lower() for word in re.findall(r"\b[A-Za-z][A-Za-z'-]{2,}\b", text or "")
        if word.lower() not in stop
    }


def _image_description_matches(query: str, description: str) -> bool:
    """Use provider metadata to reject clearly unrelated stock results."""
    query_terms = _relevance_terms(query)
    description_terms = _relevance_terms(description)
    if not query_terms or not description_terms:
        return True
    return bool(query_terms & description_terms) or any(
        len(left) >= 5 and len(right) >= 5 and left[:5] == right[:5]
        for left in query_terms for right in description_terms
    )


def _fetch_photo(story: Story) -> Optional[Image.Image]:
    # Category → reliable generic image keywords
    _CATEGORY_FALLBACK: dict[str, str] = {
        "breaking":      "breaking news",
        "war":           "conflict soldiers",
        "world":         "world globe",
        "politics":      "parliament government",
        "crime":         "police justice",
        "technology":    "technology digital",
        "business":      "business economy",
        "science":       "science laboratory",
        "sports":        "sports stadium",
        "entertainment": "theatre stage performance",
        "climate":       "climate nature environment",
        "wellness":      "health wellness",
        "jobs":          "career office work",
        "trending":      "crowd people viral",
    }

    if config.PEXELS_API_KEY:
        kw = _keywords(story.title)
        photo = _pexels(kw)
        if photo is None:
            broad = _keywords(story.title, broad=True)
            logger.warning("Pexels: no result for '%s', trying '%s'", kw, broad)
            photo = _pexels(broad)
        if photo is None:
            # Final fallback: use category keyword
            cat = getattr(story, "category", "breaking")
            cat_kw = _CATEGORY_FALLBACK.get(cat, "world news")
            logger.warning("Pexels: no result for '%s', trying category fallback '%s'", broad, cat_kw)
            photo = _pexels(cat_kw)
        if photo:
            return photo
        logger.warning("Pexels unavailable — trying Pollinations.ai")
    return _pollinations(story.title)


def _pexels(query: str, page: int = 1) -> Optional[Image.Image]:
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": config.PEXELS_API_KEY},
            params={"query": query, "per_page": 5, "page": page, "orientation": "portrait", "size": "large"},
            timeout=10,
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if not photos:
            return None
        relevant = [
            photo for photo in photos
            if _image_description_matches(query, photo.get("alt", ""))
        ]
        if not relevant:
            logger.warning("Pexels returned %d images, but metadata did not match '%s'.", len(photos), query)
            return None
        chosen = relevant[0]
        url = chosen["src"]["large2x"]
        logger.info("Pexels relevant image (page %d) by %s", page, chosen.get("photographer", "unknown"))
        return _download(url)
    except Exception as exc:
        logger.warning("Pexels error: %s", exc)
        return None


def _pollinations(title: str) -> Optional[Image.Image]:
    try:
        prompt = requests.utils.quote(
            f"news photography editorial {title[:100]} photorealistic professional"
        )
        url = (
            f"https://image.pollinations.ai/prompt/{prompt}"
            f"?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&nologo=true"
        )
        logger.info("Generating image via Pollinations.ai...")
        return _download(url, timeout=30)
    except Exception as exc:
        logger.error("Pollinations.ai error: %s", exc)
        return None


def _download(url: str, timeout: int = 15) -> Optional[Image.Image]:
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "").lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError(f"unexpected content type: {content_type}")
        if len(r.content) > 15 * 1024 * 1024:
            raise ValueError("image exceeds 15 MB")
        image = Image.open(BytesIO(r.content)).convert("RGB")
        if image.width < 600 or image.height < 600:
            raise ValueError(f"image is too small: {image.width}x{image.height}")
        return image
    except Exception as exc:
        logger.error("Image download failed: %s", exc)
        return None


# ── Composition ───────────────────────────────────────────────────────────────

def _compose(photo: Image.Image, story: Story, strong_gradients: bool = False) -> Image.Image:
    category = card_topic(story)
    if category not in CATEGORY_LABELS:
        category = "news"
    accent = BRAND_ACCENT
    label = CATEGORY_LABELS[category]

    canvas = _smart_crop(photo, IMAGE_WIDTH, IMAGE_HEIGHT).copy()
    draw = ImageDraw.Draw(canvas, "RGBA")

    top_alpha = 255 if strong_gradients else 230
    mid_alpha = 160 if strong_gradients else 40
    _gradient_rect(draw, 0, 0, IMAGE_WIDTH, int(IMAGE_HEIGHT * 0.45),
                   top_color=(*NAVY, top_alpha), bottom_color=(*NAVY, mid_alpha))
    _gradient_rect(draw, 0, int(IMAGE_HEIGHT * 0.55), IMAGE_WIDTH, IMAGE_HEIGHT,
                   top_color=(*NAVY, 0), bottom_color=(*NAVY, 255))
    _gradient_rect(draw, 0, IMAGE_HEIGHT - 300, IMAGE_WIDTH, IMAGE_HEIGHT,
                   top_color=(*NAVY, 0), bottom_color=(*NAVY, 210))
    draw.rectangle([(0, 0), (IMAGE_WIDTH, 5)], fill=(*accent, 255))
    draw.rectangle([(0, IMAGE_HEIGHT - 5), (IMAGE_WIDTH, IMAGE_HEIGHT)], fill=(*accent, 255))

    canvas = canvas.convert("RGB")
    draw = ImageDraw.Draw(canvas)

    lbl_font = _font("Montserrat-Bold.ttf", LABEL_SIZE)
    lbl_w = int(draw.textlength(label, font=lbl_font))
    lbl_px, lbl_py = 18, 8
    bx1, by1 = ML, MT
    bx2 = bx1 + lbl_w + lbl_px * 2
    by2 = by1 + LABEL_SIZE + lbl_py * 2
    draw.rectangle([(bx1 + 3, by1 + 3), (bx2 + 3, by2 + 3)], fill=(0, 0, 0))
    draw.rectangle([(bx1, by1), (bx2, by2)], fill=NAVY_LIGHT)
    draw.rectangle([(bx1, by1), (bx1 + 5, by2)], fill=WHITE)
    draw.text((bx1 + lbl_px, by1 + lbl_py), label, font=lbl_font, fill=WHITE)

    headline = getattr(story, "card_headline", None) or _short_headline(story.title)
    impact_words = _find_impact_words(headline)
    max_w = IMAGE_WIDTH - ML - MR
    hl_size = HEADLINE_SIZE
    hl_font = _font("Montserrat-ExtraBold.ttf", hl_size)
    lines = _wrap_text(draw, headline, hl_font, max_w).splitlines()
    while (len(lines) > 3 or len(lines) * int(hl_size * 1.2) > 310) and hl_size > HEADLINE_MIN:
        hl_size -= 4
        hl_font = _font("Montserrat-ExtraBold.ttf", hl_size)
        lines = _wrap_text(draw, headline, hl_font, max_w).splitlines()
    if len(lines) > 3:
        lines = lines[:3]
        logger.warning("Headline forced to 3 lines: %s", story.title)

    hl_y = by2 + 24
    line_h = int(hl_size * 1.2)
    remaining_impacts = list(impact_words)
    for line in lines:
        for ox, oy in ((3, 3), (2, 2), (1, 1)):
            draw.text((ML + ox, hl_y + oy), line, font=hl_font, fill=(0, 0, 0))
        draw.text((ML, hl_y), line, font=hl_font, fill=WHITE)
        if remaining_impacts:
            x_cursor = ML
            for word in line.split():
                word_width = int(draw.textlength(word + " ", font=hl_font))
                matched = next((item for item in remaining_impacts if word.upper() == item.upper()), None)
                if matched:
                    pad = 6
                    space_width = int(draw.textlength(" ", font=hl_font))
                    draw.rectangle(
                        [(x_cursor - pad, hl_y - 2),
                         (x_cursor + word_width - space_width + pad, hl_y + hl_size + 2)],
                        fill=RED_DARK,
                    )
                    draw.text((x_cursor, hl_y), word, font=hl_font, fill=WHITE)
                    remaining_impacts.remove(matched)
                x_cursor += word_width
        hl_y += line_h

    rule_y = hl_y + 10
    draw.rectangle([(ML, rule_y), (ML + 120, rule_y + 4)], fill=accent)

    context = _context_line(story)
    context_y = IMAGE_HEIGHT - 315
    if context:
        kicker_font = _font("Montserrat-ExtraBold.ttf", 21)
        ctx_font = _font("Montserrat-SemiBold.ttf", CONTEXT_SIZE)
        draw.rectangle([(ML, context_y), (ML + 178, context_y + 34)], fill=RED_DARK)
        draw.text((ML + 14, context_y + 5), "WHY IT MATTERS", font=kicker_font, fill=WHITE)
        wrapped = _wrap_text(draw, context, ctx_font, max_w).splitlines()[:2]
        draw.multiline_text((ML, context_y + 48), "\n".join(wrapped),
                            font=ctx_font, fill=WHITE, spacing=8)

    footer_mid = IMAGE_HEIGHT - 95
    logo_path = Path(__file__).parent.parent / "assets" / "logo.png"
    logo_h = logo_w = 100
    logo_right = ML
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA").resize((logo_w, logo_h), Image.LANCZOS)
        logo_y = footer_mid - logo_h // 2
        canvas.paste(logo, (ML, logo_y), logo.split()[3])
        logo_right = ML + logo_w

    div_x = logo_right + 20
    div_y1 = footer_mid - 38
    div_y2 = footer_mid + 38
    for offset, alpha in ((2, 40), (1, 90), (0, 200)):
        draw.rectangle([(div_x - offset, div_y1), (div_x + offset, div_y2)],
                       fill=(*accent[:3], alpha))

    sources = [_source_display_name(story.source_name, story.source_url)]
    if story.corroborating_sources:
        corroborator = story.corroborating_sources[0]
        extra = _source_display_name(corroborator.get("name", ""), corroborator.get("url", ""))
        if extra and extra not in sources:
            sources.append(extra)

    src_x = div_x + 24
    source_label_font = _font("Montserrat-Bold.ttf", 19)
    source_font = _font("Montserrat-ExtraBold.ttf", SOURCE_SIZE + 3)
    date_str = datetime.now(timezone.utc).strftime("%d %b %Y").upper()
    date_font = _font("Montserrat-Bold.ttf", DATE_SIZE + 4)
    date_w = int(draw.textlength(date_str, font=date_font))
    source_text = "  •  ".join(sources[:2]).upper()
    src_max_w = IMAGE_WIDTH - MR - src_x - date_w - 52
    while draw.textlength(source_text, font=source_font) > src_max_w and len(source_text) > 20:
        source_text = source_text[:-4].rstrip() + "..."
    draw.text((src_x, footer_mid - 34), "VERIFIED SOURCES", font=source_label_font, fill=accent)
    draw.text((src_x, footer_mid - 5), source_text, font=source_font, fill=WHITE)
    draw.text((IMAGE_WIDTH - MR - date_w, footer_mid - (DATE_SIZE + 4) // 2),
              date_str, font=date_font, fill=OFF_WHITE)
    return canvas

def _draw_rounded_rect(draw, x1, y1, x2, y2, radius, fill):
    """Fill a rounded rectangle (RGBA fill tuple)."""
    r = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
    draw.rectangle([(x1 + r, y1), (x2 - r, y2)], fill=fill)
    draw.rectangle([(x1, y1 + r), (x2, y2 - r)], fill=fill)
    draw.ellipse([(x1, y1), (x1 + r*2, y1 + r*2)], fill=fill)
    draw.ellipse([(x2 - r*2, y1), (x2, y1 + r*2)], fill=fill)
    draw.ellipse([(x1, y2 - r*2), (x1 + r*2, y2)], fill=fill)
    draw.ellipse([(x2 - r*2, y2 - r*2), (x2, y2)], fill=fill)


def _draw_rounded_rect_outline(draw, x1, y1, x2, y2, radius, outline, width=2):
    """Draw rounded rectangle outline only."""
    r = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
    draw.arc([(x1, y1), (x1 + r*2, y1 + r*2)], 180, 270, fill=outline, width=width)
    draw.arc([(x2 - r*2, y1), (x2, y1 + r*2)], 270, 360, fill=outline, width=width)
    draw.arc([(x1, y2 - r*2), (x1 + r*2, y2)], 90, 180, fill=outline, width=width)
    draw.arc([(x2 - r*2, y2 - r*2), (x2, y2)], 0, 90, fill=outline, width=width)
    draw.line([(x1 + r, y1), (x2 - r, y1)], fill=outline, width=width)
    draw.line([(x1 + r, y2), (x2 - r, y2)], fill=outline, width=width)
    draw.line([(x1, y1 + r), (x1, y2 - r)], fill=outline, width=width)
    draw.line([(x2, y1 + r), (x2, y2 - r)], fill=outline, width=width)


def _gradient_rect(
    draw: ImageDraw.Draw,
    x1: int, y1: int, x2: int, y2: int,
    top_color: tuple, bottom_color: tuple,
) -> None:
    """Draw a vertical linear gradient rectangle (RGBA draw context required)."""
    h = y2 - y1
    if h <= 0:
        return
    tr, tg, tb, ta = top_color
    br, bg, bb, ba = bottom_color
    for i in range(h):
        t = i / h
        r = int(tr + (br - tr) * t)
        g = int(tg + (bg - tg) * t)
        b = int(tb + (bb - tb) * t)
        a = int(ta + (ba - ta) * t)
        draw.line([(x1, y1 + i), (x2, y1 + i)], fill=(r, g, b, a))


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common HTML entities from a string."""
    import re, html
    text = re.sub(r"<[^>]+>", " ", text)       # strip tags
    text = html.unescape(text)                  # decode &amp; &nbsp; etc.
    text = re.sub(r"\s+", " ", text).strip()    # collapse whitespace
    return text


def _context_line(story: Story) -> str:
    """Choose a grounded consequence or key detail that does not repeat the title."""
    if getattr(story, "card_description", None):
        return story.card_description

    evidence = [story.raw_summary or "", story.article_text or ""]
    if story.post_content:
        evidence.insert(0, story.post_content)
    evidence.extend(source.get("summary", "") for source in story.corroborating_sources or [])

    title_terms = set(re.findall(r"[a-z0-9]+", story.title.lower()))
    cues = {
        "ban", "blocked", "could", "expected", "first", "impact", "marks",
        "millions", "new", "response", "risk", "shift", "trade", "would",
    }
    candidates: list[tuple[float, str]] = []
    for block in evidence:
        clean = _strip_html(block)
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", clean):
            sentence = re.sub(r"^[^A-Za-z0-9]+", "", sentence).strip()
            words = sentence.split()
            if len(words) < 6 or sentence.endswith("?") or sentence.lower().startswith("sources:"):
                continue
            terms = set(re.findall(r"[a-z0-9]+", sentence.lower()))
            overlap = len(terms & title_terms) / max(1, len(title_terms))
            score = sum(2 for cue in cues if cue in terms)
            score += 3 if re.search(r"\b\d[\d,.]*%?\b|[$£€]", sentence) else 0
            score += min(len(words), 18) / 18
            score -= overlap * 8
            candidates.append((score, sentence))

    if not candidates:
        return ""
    chosen = max(candidates, key=lambda item: item[0])[1]
    words = chosen.split()
    limit = 18
    return " ".join(words[:limit]) + ("..." if len(words) > limit else "")


def _source_display_name(name: str, url: str) -> str:
    """Return a clean publisher name suitable for compact card attribution."""
    domain = urlparse(url or "").netloc.lower().removeprefix("www.")
    known = {
        "nytimes.com": "New York Times",
        "washingtonpost.com": "Washington Post",
        "bbc.com": "BBC News",
        "bbc.co.uk": "BBC News",
        "reuters.com": "Reuters",
        "apnews.com": "Associated Press",
        "aljazeera.com": "Al Jazeera",
        "theguardian.com": "The Guardian",
    }
    for suffix, display in known.items():
        if domain == suffix or domain.endswith("." + suffix):
            return display
    clean = re.split(r"\s*[>|/]\s*", name or "")[0].strip()
    return clean or domain.split(".")[0].replace("-", " ").title() or "News Source"


def _salient_focus(img: Image.Image) -> tuple[float, float]:
    """Estimate the important visual region using edges and colour contrast."""
    import numpy as np

    sample = img.convert("RGB")
    sample.thumbnail((360, 360), Image.LANCZOS)
    arr = np.asarray(sample, dtype=np.float32)
    gray = arr.mean(axis=2)
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    saturation = arr.max(axis=2) - arr.min(axis=2)
    weights = gx + gy + saturation * 0.25

    # Ignore frame edges, watermarks, and borders.
    margin_x = max(1, sample.width // 20)
    margin_y = max(1, sample.height // 20)
    weights[:margin_y, :] = 0
    weights[-margin_y:, :] = 0
    weights[:, :margin_x] = 0
    weights[:, -margin_x:] = 0
    total = float(weights.sum())
    if total <= 0:
        return 0.5, 0.5
    ys, xs = np.indices(weights.shape)
    return float((xs * weights).sum() / total / sample.width), float((ys * weights).sum() / total / sample.height)


def _smart_crop(img: Image.Image, tw: int, th: int) -> Image.Image:
    """Crop around the most visually important region instead of blindly centering."""
    focus_x, focus_y = _salient_focus(img)
    w, h = img.size
    scale  = max(tw / w, th / h)
    nw, nh = int(w * scale), int(h * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    focus_x *= nw
    focus_y *= nh
    left = int(max(0, min(nw - tw, focus_x - tw * 0.5)))
    # Keep the subject slightly below centre, away from the headline block.
    top = int(max(0, min(nh - th, focus_y - th * 0.58)))
    return img.crop((left, top, left + tw, top + th))


def _wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    words, lines, cur = text.split(), [], ""
    for word in words:
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _short_headline(title: str) -> str:
    for sep in [" - ", " | ", " — ", " – "]:
        if sep in title:
            title = title[:title.rfind(sep)].strip()
    words = title.split()
    if len(words) <= 12:
        return title.upper()
    return " ".join(words[:10]).upper() + "..."


def _keywords(title: str, broad: bool = False) -> str:
    stop = {
        "the","a","an","and","or","but","in","on","at","to","for","of","with",
        "by","from","as","is","was","are","were","be","been","its","this","that",
        "says","said","new","more","than","up","out","after","before","about",
        "just","over","not","no","how","why","when","where","who","what","which",
    }
    words = [
        w.strip(".,!?:;\"'()[]—–-")
        for w in title.lower().split()
        if w.strip(".,!?:;\"'()[]—–-") not in stop and len(w) > 2
    ]
    if broad:
        return words[0] if words else "world news"
    return " ".join(words[:3]) if words else "world news"
