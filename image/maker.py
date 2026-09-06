"""
image/maker.py — Step 2: Professional Facebook news post image generator.

Design: Full-bleed photo with gradient overlays, editorial style.
Canvas: 1200 × 1500 px (4:5 mobile-first)

Fallback chain:
  Pexels (keyword) → Pexels (broad) → Pollinations.ai → None (text-only post)
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

def _find_impact_word(headline: str) -> Optional[str]:
    """
    Find the single most impactful word in the headline.
    Returns the word as it appears in the headline (preserving case),
    or None if no impact word is found.
    """
    words = re.findall(r"[A-Za-z']+", headline)
    upper_words = [w.upper() for w in words]

    for _tier, word_list in _IMPACT_WORDS:
        for impact in word_list:
            if impact in upper_words:
                idx = upper_words.index(impact)
                return words[idx]   # return original casing from headline
    return None
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
      If all 3 fail → log all failures and return None (text-only post).
    """
    IMAGES_DIR = Path("images")
    IMAGES_DIR.mkdir(exist_ok=True)

    photo = _fetch_photo(story)
    if photo is None:
        logger.error("All image sources failed for: %s", story.title)
        return None

    for attempt in range(3):
        if attempt == 0:
            current_photo    = photo
            strong_gradients = False
        elif attempt == 1:
            # Same photo — push gradients harder to fix contrast failure
            current_photo    = photo
            strong_gradients = True
            logger.warning("Mobile check failed — retrying with stronger gradients")
        else:
            # Fresh photo with a broader keyword
            logger.warning("Mobile check failed again — fetching new photo")
            current_photo = _fetch_photo(story) or photo
            strong_gradients = False

        image  = _compose(current_photo, story, strong_gradients=strong_gradients)
        issues = _mobile_visibility_check(image, story, raw_photo=current_photo)

        if not issues:
            # All checks passed
            safe     = "".join(c if c.isalnum() or c in "-_" else "_" for c in story.title[:45])
            out_path = IMAGES_DIR / f"{safe}.jpg"
            image.save(out_path, "JPEG", quality=93, optimize=True)
            logger.info("Image saved (attempt %d): %s", attempt + 1, out_path)
            return out_path

        # Log exactly which checks failed and why
        fail_summary = "; ".join(f"{k}: {v}" for k, v in issues.items())
        logger.warning("Mobile check attempt %d failed — %s", attempt + 1, fail_summary)

    logger.error(
        "Image rejected after 3 attempts (mobile checks): %s | issues: %s",
        story.title, fail_summary,
    )
    return None


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
    # Headline sits in top overlay zone: rows 0..int(h*0.42), left margin to right.
    # Compare mean brightness of the leftmost 80% of that zone.
    hl_zone = arr[MT : MT + MOBILE_MAX_HEADLINE_HEIGHT, ML : w - MR]
    if hl_zone.size > 0:
        brightness = hl_zone.mean(axis=2)        # (H, W) grayscale
        mean_dark  = brightness.mean()
        std_bright = brightness.std()
        # We want DARK background (mean < 160) AND enough variance for white text to pop
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

def _fetch_photo(story: Story) -> Optional[Image.Image]:
    if config.PEXELS_API_KEY:
        kw = _keywords(story.title)
        photo = _pexels(kw)
        if photo is None:
            broad = _keywords(story.title, broad=True)
            logger.warning("Pexels: no result for '%s', trying '%s'", kw, broad)
            photo = _pexels(broad)
        if photo:
            return photo
        logger.warning("Pexels unavailable — trying Pollinations.ai")
    return _pollinations(story.title)


def _pexels(query: str) -> Optional[Image.Image]:
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": config.PEXELS_API_KEY},
            params={"query": query, "per_page": 5, "orientation": "portrait", "size": "large"},
            timeout=10,
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if not photos:
            return None
        url = photos[0]["src"]["large2x"]
        logger.info("Pexels image by %s", photos[0].get("photographer", ""))
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
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as exc:
        logger.error("Image download failed: %s", exc)
        return None


# ── Composition ───────────────────────────────────────────────────────────────

def _compose(photo: Image.Image, story: Story, strong_gradients: bool = False) -> Image.Image:
    category = (getattr(story, "category", "") or "").lower().strip()
    if category not in CATEGORY_LABELS:
        category = "news"
    accent    = BRAND_ACCENT
    dot_color = CATEGORY_DOT_COLORS[category]
    label     = CATEGORY_LABELS[category]

    # ── 1. Full-bleed photo as base ───────────────────────────────────────────
    canvas = _smart_crop(photo, IMAGE_WIDTH, IMAGE_HEIGHT).copy()
    draw   = ImageDraw.Draw(canvas, "RGBA")

    # ── 2. Top gradient overlay ───────────────────────────────────────────────
    # strong_gradients: heavier opacity to fix contrast failure on bright photos
    top_alpha    = 255 if strong_gradients else 230
    mid_alpha    = 180 if strong_gradients else 0
    _gradient_rect(draw, 0, 0, IMAGE_WIDTH, int(IMAGE_HEIGHT * 0.42),
                   top_color=(*NAVY, top_alpha), bottom_color=(*NAVY, mid_alpha))

    # ── 3. Bottom gradient overlay ────────────────────────────────────────────
    fade_start = int(IMAGE_HEIGHT * 0.58)
    _gradient_rect(draw, 0, fade_start, IMAGE_WIDTH, IMAGE_HEIGHT,
                   top_color=(*NAVY, 0), bottom_color=(*NAVY, 255))

    # ── 4. Extra bottom darkening for footer readability ─────────────────────
    bottom_extra = 255 if strong_gradients else 210
    _gradient_rect(draw, 0, IMAGE_HEIGHT - 280, IMAGE_WIDTH, IMAGE_HEIGHT,
                   top_color=(*NAVY, 0), bottom_color=(*NAVY, bottom_extra))

    # ── 5. Accent color top border line ───────────────────────────────────────
    draw.rectangle([(0, 0), (IMAGE_WIDTH, 6)], fill=(*accent, 255))

    # ── 6. Thin accent line at bottom edge ────────────────────────────────────
    draw.rectangle([(0, IMAGE_HEIGHT - 6), (IMAGE_WIDTH, IMAGE_HEIGHT)],
                   fill=(*accent, 255))

    # ── 7. Left accent bar (vertical stripe, full height of top area) ─────────
    draw.rectangle([(0, 0), (6, PHOTO_BOT)], fill=(*accent, 140))

    # Commit RGBA draws, switch to RGB draw for text
    canvas = canvas.convert("RGB")
    draw   = ImageDraw.Draw(canvas)

    # ── 8. Category label badge ───────────────────────────────────────────────
    lbl_font = _font("Montserrat-Bold.ttf", LABEL_SIZE)
    lbl_w    = int(draw.textlength(label, font=lbl_font))
    lbl_px, lbl_py = 20, 9
    DOT_R = 7   # radius of the per-category color dot

    bx1 = ML
    by1 = MT
    bx2 = bx1 + DOT_R * 2 + 10 + lbl_w + lbl_px * 2
    by2 = by1 + LABEL_SIZE + lbl_py * 2

    # Touch #3 — Badge drop shadow: lifts badge off any photo background
    shadow_offset = 4
    draw.rectangle(
        [(bx1 + shadow_offset, by1 + shadow_offset),
         (bx2 + shadow_offset, by2 + shadow_offset)],
        fill=(0, 0, 0),
    )

    # Badge: navy fill (same on every card — brand identity)
    draw.rectangle([(bx1, by1), (bx2, by2)], fill=NAVY_LIGHT)
    # White left stripe on badge
    draw.rectangle([(bx1, by1), (bx1 + 6, by2)], fill=WHITE)

    # Category dot — the ONLY per-category color on the whole card
    dot_cx = bx1 + lbl_px + DOT_R
    dot_cy = (by1 + by2) // 2
    draw.ellipse(
        [(dot_cx - DOT_R, dot_cy - DOT_R), (dot_cx + DOT_R, dot_cy + DOT_R)],
        fill=dot_color,
    )

    # Label text — white
    draw.text((bx1 + lbl_px + DOT_R * 2 + 10, by1 + lbl_py), label, font=lbl_font, fill=WHITE)

    label_bottom = by2

    # ── 9. Main headline ──────────────────────────────────────────────────────
    headline     = _short_headline(story.title)
    impact_word  = _find_impact_word(headline)   # strongest word to highlight
    max_w        = IMAGE_WIDTH - ML - MR
    hl_size      = HEADLINE_SIZE
    hl_font      = _font("Montserrat-ExtraBold.ttf", hl_size)
    wrapped      = _wrap_text(draw, headline, hl_font, max_w)
    lines        = wrapped.splitlines()

    while (len(lines) > 3 or len(lines) * int(hl_size * 1.2) > 290) and hl_size > HEADLINE_MIN:
        hl_size -= 4
        hl_font  = _font("Montserrat-ExtraBold.ttf", hl_size)
        wrapped  = _wrap_text(draw, headline, hl_font, max_w)
        lines    = wrapped.splitlines()

    # Hard cap: never render more than 3 lines regardless of font size
    if len(lines) > 3:
        lines = lines[:3]
        logger.warning("Headline forced to 3 lines: %s", story.title)

    line_h = int(hl_size * 1.2)
    hl_y   = label_bottom + 22

    for line in lines[:3]:
        # Multi-layer shadow for depth
        for ox, oy in [(3, 3), (2, 2), (1, 1)]:
            draw.text((ML + ox, hl_y + oy), line, font=hl_font, fill=(0, 0, 0))
        draw.text((ML, hl_y), line, font=hl_font, fill=WHITE)

        # Touch #1 — Impact word highlight: red box behind the strongest word
        # Drawn after the white text so the highlight sits visually behind the word
        # We redraw just that word on top after painting the highlight box
        if impact_word and impact_word.upper() in line.upper():
            words_in_line = line.split()
            x_cursor = ML
            for w in words_in_line:
                w_width = int(draw.textlength(w + " ", font=hl_font))
                if w.upper() == impact_word.upper():
                    pad = 6
                    # Draw the red highlight box
                    draw.rectangle(
                        [(x_cursor - pad, hl_y - 2),
                         (x_cursor + w_width - int(draw.textlength(" ", font=hl_font)) + pad,
                          hl_y + hl_size + 2)],
                        fill=RED_DARK,
                    )
                    # Redraw the word on top of the highlight
                    draw.text((x_cursor, hl_y), w, font=hl_font, fill=WHITE)
                    impact_word = None   # only highlight once across all lines
                    break
                x_cursor += w_width

        hl_y += line_h

    # Touch #4 — Rule: thicker (5px) with a subtle full-width navy track behind it
    rule_y = hl_y + 12
    draw.rectangle([(ML, rule_y), (IMAGE_WIDTH - MR, rule_y + 5)], fill=(*NAVY_LIGHT, 120))
    draw.rectangle([(ML, rule_y), (ML + 140, rule_y + 5)], fill=accent)

    # ── 11. Context line — sits inside bottom of photo, above footer ─────────
    # Position: 280px above bottom edge, left-aligned with a accent bar
    context = _context_line(story)
    CONTEXT_Y = IMAGE_HEIGHT - 280   # above footer row (footer mid = HEIGHT - 95)

    if context:
        ctx_font = _font("Montserrat-SemiBold.ttf", CONTEXT_SIZE)
        while draw.textlength(context, font=ctx_font) > max_w and len(context) > 10:
            context = context[:context.rfind(" ")] + "..."

        ctx_h = CONTEXT_SIZE
        # Thin accent bar to the left of the context line
        draw.rectangle(
            [(ML, CONTEXT_Y), (ML + 4, CONTEXT_Y + ctx_h)],
            fill=accent,
        )
        # Context text indented after bar
        draw.text((ML + 16, CONTEXT_Y), context, font=ctx_font, fill=OFF_WHITE)

        # Thin full-width separator line below context, above footer
        sep_y = CONTEXT_Y + ctx_h + 18
        draw.rectangle([(ML, sep_y), (IMAGE_WIDTH - MR, sep_y + 1)],
                       fill=(255, 255, 255, 40))

    # ── 12. Footer — NO background, elements float over the gradient ──────────
    FOOTER_MID = IMAGE_HEIGHT - 95

    # Touch #2 — Footer separator: thin white line across full width above footer
    sep_footer_y = IMAGE_HEIGHT - 180
    draw.rectangle(
        [(ML, sep_footer_y), (IMAGE_WIDTH - MR, sep_footer_y + 1)],
        fill=(255, 255, 255),
    )

    # ── Logo ──────────────────────────────────────────────────────────────────
    logo_path = Path(__file__).parent.parent / "assets" / "logo.png"
    LOGO_H    = 110
    LOGO_W    = 110
    logo_right = ML

    if logo_path.exists():
        logo   = Image.open(logo_path).convert("RGBA")
        logo   = logo.resize((LOGO_W, LOGO_H), Image.LANCZOS)
        logo_y = FOOTER_MID - LOGO_H // 2
        canvas.paste(logo, (ML, logo_y), logo.split()[3])
        logo_right = ML + LOGO_W

        # Touch #5 — Wordmark: "GLOBAL PULSE NEWS" stacked below logo name
        # Small, off-white, always visible even if logo image fails to render
        wm_font  = _font("Montserrat-Bold.ttf", 18)
        wm_text  = "GLOBAL PULSE NEWS"
        wm_x     = ML
        wm_y     = logo_y + LOGO_H + 4
        draw.text((wm_x, wm_y), wm_text, font=wm_font, fill=(*OFF_WHITE, 180))
    else:
        # No logo — show just the wordmark prominently
        wm_font  = _font("Montserrat-ExtraBold.ttf", 26)
        wm_text  = "GLOBAL PULSE NEWS"
        wm_x     = ML
        wm_y     = FOOTER_MID - 13
        draw.text((wm_x, wm_y), wm_text, font=wm_font, fill=WHITE)
        logo_right = ML + int(draw.textlength(wm_text, font=wm_font))

    # ── Glowing vertical divider ──────────────────────────────────────────────
    div_x  = logo_right + 20
    div_y1 = FOOTER_MID - 40
    div_y2 = FOOTER_MID + 40
    # Soft glow: draw 3px wide with decreasing opacity
    for offset, alpha in [(2, 40), (1, 90), (0, 200)]:
        c = tuple([*accent[:3], alpha])
        draw.rectangle(
            [(div_x - offset, div_y1), (div_x + offset, div_y2)],
            fill=(*accent[:3], alpha)
        )

    # ── Source section ────────────────────────────────────────────────────────
    sources = [story.source_name]
    if story.corroborating_sources:
        extra = story.corroborating_sources[0].get("name", "")
        if extra and extra != story.source_name:
            sources.append(extra)

    src_x     = div_x + 24
    pri_font  = _font("Montserrat-ExtraBold.ttf", SOURCE_SIZE + 6)  # primary source — big
    sec_font  = _font("Montserrat-Medium.ttf",    SOURCE_SIZE)       # secondary — smaller
    line_gap  = 10
    pri_h     = SOURCE_SIZE + 6
    sec_h     = SOURCE_SIZE
    total_h   = pri_h + (line_gap + sec_h if len(sources) > 1 else 0)
    sy        = FOOTER_MID - total_h // 2

    # ── Primary source ────────────────────────────────────────────────────────
    # Accent dot
    dot_r  = 6
    dot_cx = src_x + dot_r
    dot_cy = sy + pri_h // 2
    draw.ellipse([(dot_cx - dot_r, dot_cy - dot_r),
                  (dot_cx + dot_r, dot_cy + dot_r)], fill=accent)

    pri_name = sources[0].upper()
    # Gap 3 fix: truncate source name so it never overflows past right margin
    src_max_w = IMAGE_WIDTH - MR - (src_x + dot_r * 2 + 14) - 20
    while draw.textlength(pri_name, font=pri_font) > src_max_w and len(pri_name) > 4:
        pri_name = pri_name[:-2].rstrip() + "."
    pri_w = int(draw.textlength(pri_name, font=pri_font))

    # Primary name — white, extrabold
    draw.text((src_x + dot_r * 2 + 14, sy), pri_name, font=pri_font, fill=WHITE)

    # Thin accent underline under primary name
    ul_x = src_x + dot_r * 2 + 14
    draw.rectangle(
        [(ul_x, sy + pri_h + 2), (ul_x + pri_w, sy + pri_h + 4)],
        fill=accent,
    )

    # ── Secondary source ──────────────────────────────────────────────────────
    if len(sources) > 1:
        sy2      = sy + pri_h + line_gap + 4
        sec_name = sources[1].upper()

        # Smaller hollow dot
        dot_r2  = 4
        dot_cx2 = src_x + dot_r2
        dot_cy2 = sy2 + sec_h // 2
        draw.ellipse([(dot_cx2 - dot_r2, dot_cy2 - dot_r2),
                      (dot_cx2 + dot_r2, dot_cy2 + dot_r2)],
                     outline=accent, width=2)

        draw.text((src_x + dot_r2 * 2 + 14, sy2), sec_name,
                  font=sec_font, fill=OFF_WHITE)

        sy += line_h

    # ── Date (right side, clean — no wordmark) ────────────────────────────────
    date_str  = datetime.now(timezone.utc).strftime("%d %b %Y").upper()
    date_font = _font("Montserrat-ExtraBold.ttf", DATE_SIZE + 6)
    date_w    = int(draw.textlength(date_str, font=date_font))
    date_x    = IMAGE_WIDTH - MR - date_w
    date_y    = FOOTER_MID - (DATE_SIZE + 6) // 2

    # Subtle accent glow behind date
    for gx, gy, ga in [(-1,-1,50),(1,-1,50),(-1,1,50),(1,1,50)]:
        draw.text((date_x + gx, date_y + gy), date_str, font=date_font,
                  fill=(*accent[:3], ga))
    draw.text((date_x, date_y), date_str, font=date_font, fill=WHITE)

    # Accent underline below date
    draw.rectangle(
        [(date_x, date_y + DATE_SIZE + 9), (date_x + date_w, date_y + DATE_SIZE + 12)],
        fill=accent,
    )

    return canvas


# ── Drawing helpers ───────────────────────────────────────────────────────────

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
    return " ".join(words[:12]) + ("..." if len(words) > 12 else "")


def _smart_crop(img: Image.Image, tw: int, th: int) -> Image.Image:
    w, h   = img.size
    scale  = max(tw / w, th / h)
    nw, nh = int(w * scale), int(h * scale)
    img    = img.resize((nw, nh), Image.LANCZOS)
    left   = (nw - tw) // 2
    top    = (nh - th) // 2
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
