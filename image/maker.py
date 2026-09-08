"""
image/maker.py — Step 2: Professional Facebook news post image generator.

Design: Full-bleed photo with gradient overlays, editorial style.
Canvas: 1200 × 1500 px (4:5 mobile-first)

Fallback chain:
  Article image → relevant Pexels image → category fallback → draft hold
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import config
from models import Story

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
      Attempt 2 — same photo, stronger gradient overlays (boost contrast).
      Attempt 3 — new photo fetch (different keyword), standard overlays.
      If all attempts fail → return None so the story remains a draft.
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
    last_photo   = None

    for attempt, fetch_fn in enumerate(search_strategies):
        current_photo = fetch_fn()
        if current_photo is None:
            logger.warning("Image fetch strategy %d returned nothing — skipping.", attempt + 1)
            if last_photo is None:
                continue
            current_photo = last_photo  # reuse last known photo as a last resort
        last_photo = current_photo

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
    """Create a guaranteed local card when no external image is available."""
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    category = (getattr(story, "category", "") or "news").lower()
    color = CATEGORY_DOT_COLORS.get(category, CATEGORY_DOT_COLORS["news"])

    background = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), NAVY)
    draw = ImageDraw.Draw(background, "RGBA")
    for y in range(IMAGE_HEIGHT):
        t = y / max(IMAGE_HEIGHT - 1, 1)
        draw.line(
            (0, y, IMAGE_WIDTH, y),
            fill=(
                int(NAVY[0] + color[0] * t * 0.22),
                int(NAVY[1] + color[1] * t * 0.22),
                int(NAVY[2] + color[2] * t * 0.22),
                255,
            ),
        )
    # Abstract newsroom/world pattern adds depth without implying a false photo.
    for radius, alpha in ((430, 32), (330, 42), (230, 52)):
        box = (
            IMAGE_WIDTH - radius - 120,
            IMAGE_HEIGHT // 2 - radius,
            IMAGE_WIDTH + radius - 120,
            IMAGE_HEIGHT // 2 + radius,
        )
        draw.ellipse(box, outline=(*color, alpha), width=5)
    for offset in range(-500, 700, 120):
        draw.line((offset, IMAGE_HEIGHT, offset + 900, 0), fill=(255, 255, 255, 16), width=3)

    card = _compose(background, story, strong_gradients=False)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in story.title[:45])
    out_path = config.IMAGES_DIR / f"{safe}_fallback.jpg"
    card.save(out_path, "JPEG", quality=93, optimize=True)
    logger.info("✅ Branded fallback image saved: %s", out_path)
    return out_path


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
    """Compose the mobile-first Global Pulse News card."""
    category = (getattr(story, "category", "") or "").lower().strip()
    if category not in CATEGORY_LABELS:
        category = "news"
    label = CATEGORY_LABELS[category]
    dot_color = CATEGORY_DOT_COLORS[category]

    canvas = _smart_crop(photo, IMAGE_WIDTH, IMAGE_HEIGHT).convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay, "RGBA")

    # Strong contrast at the headline and footer, while keeping the subject visible.
    top_alpha = 245 if strong_gradients else 220
    _gradient_rect(odraw, 0, 0, IMAGE_WIDTH, 690,
                   top_color=(*NAVY, top_alpha), bottom_color=(*NAVY, 35))
    _gradient_rect(odraw, 0, 820, IMAGE_WIDTH, IMAGE_HEIGHT,
                   top_color=(*NAVY, 0), bottom_color=(*NAVY, 235))
    odraw.rectangle((0, 0, IMAGE_WIDTH - 1, IMAGE_HEIGHT - 1),
                    outline=(*BRAND_ACCENT, 255), width=8)

    # Bottom information panel.
    panel = (ML, IMAGE_HEIGHT - 365, IMAGE_WIDTH - MR, IMAGE_HEIGHT - 55)
    odraw.rounded_rectangle(panel, radius=26, fill=(*NAVY, 225),
                            outline=(255, 255, 255, 45), width=2)
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    # Compact brand bar.
    bar = (ML, MT, IMAGE_WIDTH - MR, MT + 100)
    draw.rounded_rectangle(bar, radius=25, fill=(*NAVY, 220),
                           outline=(255, 255, 255, 210), width=2)
    logo_path = Path(__file__).parent.parent / "assets" / "logo.png"
    logo_size = 76
    brand_x = ML + 24
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA").resize((logo_size, logo_size), Image.LANCZOS)
        canvas.alpha_composite(logo, (brand_x, MT + 12))
        brand_x += logo_size + 24
    brand_font = _font("Montserrat-Bold.ttf", 36)
    draw.text((brand_x, MT + 28), "GLOBAL PULSE NEWS", font=brand_font, fill=WHITE)

    badge_font = _font("Montserrat-SemiBold.ttf", 25)
    badge_w = int(draw.textlength(label, font=badge_font)) + 86
    badge = (IMAGE_WIDTH - MR - badge_w, MT + 22, IMAGE_WIDTH - MR - 24, MT + 78)
    draw.rounded_rectangle(badge, radius=28, fill=(*BRAND_ACCENT, 255))
    draw.ellipse((badge[0] + 22, badge[1] + 19, badge[0] + 40, badge[1] + 37), fill=dot_color)
    draw.text((badge[0] + 52, badge[1] + 12), label, font=badge_font, fill=WHITE)

    # Responsive headline with red text emphasis instead of bulky highlight boxes.
    headline = getattr(story, "card_headline", None) or _short_headline(story.title)
    impact_words = {w.upper() for w in _find_impact_words(headline)}
    max_w = IMAGE_WIDTH - ML - MR
    hl_size = 88
    while True:
        hl_font = _font("Montserrat-ExtraBold.ttf", hl_size)
        lines = _wrap_text(draw, headline, hl_font, max_w).splitlines()
        if len(lines) <= 3 or hl_size <= HEADLINE_MIN:
            break
        hl_size -= 4
    lines = lines[:3]
    y = MT + 205
    line_h = int(hl_size * 1.22)
    space_w = draw.textlength(" ", font=hl_font)
    for line in lines:
        x = ML
        line_is_emphasis = bool(
            re.search(r"(?:\d[\d,.]*%?|[$£€]\s*\d)", line)
            or any(re.sub(r"[^A-Za-z']", "", w).upper() in impact_words for w in line.split())
        )
        for word in line.split():
            clean = re.sub(r"[^A-Za-z']", "", word).upper()
            fill = BRAND_ACCENT if line_is_emphasis or clean in impact_words else WHITE
            draw.text((x + 4, y + 5), word, font=hl_font, fill=(0, 0, 0, 170))
            draw.text((x, y), word, font=hl_font, fill=fill)
            x += draw.textlength(word, font=hl_font) + space_w
        y += line_h
    draw.rounded_rectangle((ML, y + 8, ML + 178, y + 20), radius=6, fill=BRAND_ACCENT)

    # Context and source hierarchy in one stable mobile-safe panel.
    context = _context_line(story)
    topic_font = _font("Montserrat-SemiBold.ttf", 26)
    body_font = _font("Montserrat-Bold.ttf", 31)
    source_font = _font("Montserrat-SemiBold.ttf", 22)
    px, py = ML + 30, panel[1] + 38
    draw.text((px, py), label, font=topic_font, fill=BRAND_ACCENT)
    if context:
        wrapped_context = _wrap_text(draw, context, body_font, panel[2] - px - 30)
        context_lines = wrapped_context.splitlines()[:2]
        draw.multiline_text((px, py + 48), "\n".join(context_lines),
                            font=body_font, fill=WHITE, spacing=10)

    divider_y = panel[3] - 68
    draw.line((px, divider_y, panel[2] - 30, divider_y), fill=(255, 255, 255, 80), width=2)
    sources = [story.source_name]
    if story.corroborating_sources:
        extra = story.corroborating_sources[0].get("name", "")
        if extra and extra != story.source_name:
            sources.append(extra)
    source_text = "SOURCE  •  " + " / ".join(s.upper() for s in sources[:2])
    max_source_w = 700
    while draw.textlength(source_text, font=source_font) > max_source_w and len(source_text) > 20:
        source_text = source_text[:-4].rstrip() + "..."
    draw.text((px, divider_y + 22), source_text, font=source_font, fill=OFF_WHITE)
    date_text = datetime.now(timezone.utc).strftime("%d %b").upper()
    date_w = draw.textlength(date_text, font=source_font)
    draw.text((panel[2] - 30 - date_w, divider_y + 22), date_text,
              font=source_font, fill=OFF_WHITE)

    return canvas.convert("RGB")

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
    summary = _strip_html(story.raw_summary or "")
    if not summary:
        return ""
    first = summary.split(".")[0].strip()
    words = first.split()
    if len(words) < 4:
        return ""
    return " ".join(words[:20]) + ("..." if len(words) > 20 else "")


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
