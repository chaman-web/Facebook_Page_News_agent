"""
image/maker.py — Step 2: Professional Facebook news post image generator.

Design spec: Global Pulse News brand
- Canvas: 1200 × 1500 px (4:5 mobile-first)
- Brand colors: Dark navy, Red, White
- Typography: Montserrat ExtraBold (headline), Montserrat Bold (label/date)
- Layout: Category label + headline top-left, photo dominant center,
          brand name bottom-left, date bottom-right

Fallback chain:
  Pexels (keyword) → Pexels (broad) → Pollinations.ai → None (text-only post)
"""

from __future__ import annotations

import logging
import os
import textwrap
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import config
from models import Story

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canvas
# ---------------------------------------------------------------------------

IMAGE_WIDTH  = 1200
IMAGE_HEIGHT = 1500   # 4:5 ratio — optimal for Facebook mobile feed

# ---------------------------------------------------------------------------
# Brand colors  (Dark navy / Red / White)
# ---------------------------------------------------------------------------

NAVY        = (10,  20,  50)       # Dark navy background / overlays
RED         = (210, 30,  45)       # Accent red for category label
WHITE       = (255, 255, 255)
OFF_WHITE   = (240, 240, 240)      # Subtle secondary text
SHADOW      = (0,   0,   0,  180)  # Text drop shadow (RGBA)

# ---------------------------------------------------------------------------
# Layout margins & sizes
# ---------------------------------------------------------------------------

MARGIN_LEFT   = 80
MARGIN_TOP    = 70
MARGIN_RIGHT  = 80
MARGIN_BOTTOM = 75

LABEL_FONT_SIZE    = 30
HEADLINE_FONT_SIZE = 76   # Reduced if headline is long
HEADLINE_MIN_SIZE  = 58
DATE_FONT_SIZE     = 26
BRAND_FONT_SIZE    = 28

LABEL_PAD_X = 22   # Horizontal padding inside the red label box
LABEL_PAD_Y = 10   # Vertical padding inside the red label box

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"

def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a Montserrat variant; fall back to Ubuntu Bold then PIL default."""
    candidates = [
        _FONT_DIR / name,
        Path("/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except Exception:
                continue
    return ImageFont.load_default()

# ---------------------------------------------------------------------------
# Category → label text mapping
# ---------------------------------------------------------------------------

CATEGORY_LABELS = {
    "breaking":      "BREAKING NEWS",
    "technology":    "TECHNOLOGY",
    "business":      "BUSINESS",
    "politics":      "POLITICS",
    "science":       "SCIENCE & HEALTH",
    "sports":        "SPORTS",
    "trending":      "TRENDING",
    "entertainment": "ENTERTAINMENT",
}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def create_news_image(story: Story) -> Optional[Path]:
    """
    Build a professional news post image for the given story.
    Returns local path to saved JPEG, or None on failure.
    """
    IMAGES_DIR = Path("images")
    IMAGES_DIR.mkdir(exist_ok=True)

    # 1. Fetch photo
    photo = _fetch_photo(story)
    if photo is None:
        logger.error("All image sources failed for: %s", story.title)
        return None

    # 2. Compose the full graphic
    image = _compose(photo, story)

    # 3. Save
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in story.title[:45])
    out_path = IMAGES_DIR / f"{safe}.jpg"
    image.save(out_path, "JPEG", quality=93, optimize=True)
    logger.info("Image saved: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Photo fetching
# ---------------------------------------------------------------------------

def _fetch_photo(story: Story) -> Optional[Image.Image]:
    """Pexels → Pexels broad → Pollinations fallback."""
    if config.PEXELS_API_KEY:
        keywords = _keywords(story.title)
        photo = _pexels(keywords)
        if photo is None:
            broad = _keywords(story.title, broad=True)
            logger.warning("Pexels: no result for '%s', trying '%s'", keywords, broad)
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
        photographer = photos[0].get("photographer", "")
        logger.info("Pexels image by %s", photographer)
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


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def _compose(photo: Image.Image, story: Story) -> Image.Image:
    """
    Assemble the full 1200×1500 canvas following the exact layout spec:

    0–90 px       Dark navy top bar — category label
    90–280 px     MAIN HEADLINE (2–3 lines, white ExtraBold)
    280–1230 px   NEWS PHOTO (dominant, full width)
    1230–1350 px  Dark navy — optional short context line
    1350–1500 px  Dark navy bottom bar — logo left, date right
    """
    canvas = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), NAVY)
    draw = ImageDraw.Draw(canvas)

    # ── Zone boundaries ──────────────────────────────────────────────────────
    TOP_BAR_H      = 90      # category label zone
    HEADLINE_TOP   = 100     # headline starts here
    HEADLINE_BOT   = 285     # headline ends here
    PHOTO_TOP      = 285     # photo starts immediately after headline
    PHOTO_BOT      = 1235    # photo ends here  (~65% of canvas)
    CONTEXT_TOP    = 1235    # optional context line
    CONTEXT_BOT    = 1360
    BOTTOM_TOP     = 1360    # logo + date bar
    # ────────────────────────────────────────────────────────────────────────

    # --- Top bar background (slightly lighter navy for contrast) ---
    draw.rectangle([(0, 0), (IMAGE_WIDTH, TOP_BAR_H)], fill=(15, 28, 65))

    # --- Photo zone ---
    photo_h = PHOTO_BOT - PHOTO_TOP
    photo_resized = _smart_crop(photo, IMAGE_WIDTH, photo_h)
    canvas.paste(photo_resized, (0, PHOTO_TOP))

    # --- Thin red accent line between headline zone and photo ---
    draw.rectangle([(0, PHOTO_TOP - 4), (IMAGE_WIDTH, PHOTO_TOP)], fill=RED)

    # --- Context + bottom zones (solid navy) ---
    draw.rectangle([(0, PHOTO_BOT), (IMAGE_WIDTH, IMAGE_HEIGHT)], fill=NAVY)

    # --- Thin red accent line above context zone ---
    draw.rectangle([(0, PHOTO_BOT), (IMAGE_WIDTH, PHOTO_BOT + 4)], fill=RED)

    # ── Category label (top-left in top bar) ─────────────────────────────────
    category = getattr(story, "category", "breaking") or "breaking"
    label_text = CATEGORY_LABELS.get(category.lower(), "WORLD NEWS")
    label_font = _font("Montserrat-Bold.ttf", LABEL_FONT_SIZE)
    text_w = draw.textlength(label_text, font=label_font)

    lx1 = MARGIN_LEFT
    ly1 = 18
    lx2 = int(lx1 + text_w + LABEL_PAD_X * 2)
    ly2 = int(ly1 + LABEL_FONT_SIZE + LABEL_PAD_Y * 2)
    draw.rectangle([(lx1, ly1), (lx2, ly2)], fill=RED)
    draw.text((lx1 + LABEL_PAD_X, ly1 + LABEL_PAD_Y), label_text, font=label_font, fill=WHITE)

    # ── Main headline (top-left, below category bar) ──────────────────────────
    headline = _short_headline(story.title)
    max_width = IMAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    available_h = HEADLINE_BOT - HEADLINE_TOP - 20

    font_size = HEADLINE_FONT_SIZE
    font = _font("Montserrat-ExtraBold.ttf", font_size)
    wrapped = _wrap_text(draw, headline, font, max_width)
    lines = wrapped.splitlines()

    # Reduce font size until it fits in 3 lines within the headline zone
    while (len(lines) > 3 or len(lines) * int(font_size * 1.18) > available_h) and font_size > HEADLINE_MIN_SIZE:
        font_size -= 4
        font = _font("Montserrat-ExtraBold.ttf", font_size)
        wrapped = _wrap_text(draw, headline, font, max_width)
        lines = wrapped.splitlines()

    line_height = int(font_size * 1.18)
    y = HEADLINE_TOP

    for line in lines[:3]:
        # Shadow
        draw.text((MARGIN_LEFT + 2, y + 2), line, font=font, fill=(0, 0, 0, 160))
        # White text
        draw.text((MARGIN_LEFT, y), line, font=font, fill=WHITE)
        y += line_height

    # ── Optional context line (1235–1360 px zone) ─────────────────────────────
    context = _context_line(story)
    if context:
        ctx_font = _font("Montserrat-Medium.ttf", 34)
        ctx_y = CONTEXT_TOP + (CONTEXT_BOT - CONTEXT_TOP - 34) // 2
        draw.text((MARGIN_LEFT, ctx_y), context, font=ctx_font, fill=OFF_WHITE)

    # ── Bottom bar: logo left, date right (1360–1500 px) ─────────────────────
    bottom_bar_h = IMAGE_HEIGHT - BOTTOM_TOP   # 140 px
    logo_h = bottom_bar_h - 30                 # 110 px tall, 15 px padding top/bottom
    logo_w = logo_h                            # square logo

    logo_path = Path(__file__).parent.parent / "assets" / "logo.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA")
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
        logo_y = BOTTOM_TOP + (bottom_bar_h - logo_h) // 2
        if logo.mode == "RGBA":
            canvas.paste(logo, (MARGIN_LEFT, logo_y), logo.split()[3])
        else:
            canvas.paste(logo.convert("RGB"), (MARGIN_LEFT, logo_y))
    else:
        # Fallback: text if logo file missing
        brand_font = _font("Montserrat-Bold.ttf", BRAND_FONT_SIZE)
        brand_y = BOTTOM_TOP + (bottom_bar_h - BRAND_FONT_SIZE) // 2
        draw.text((MARGIN_LEFT, brand_y), "WORLD ", font=brand_font, fill=WHITE)
        world_w = int(draw.textlength("WORLD ", font=brand_font))
        draw.text((MARGIN_LEFT + world_w, brand_y), "UPDATE", font=brand_font, fill=RED)

    # Source badge — centered between logo and date
    sources = [story.source_name]
    if story.corroborating_sources:
        extra = story.corroborating_sources[0].get("name", "")
        if extra and extra != story.source_name:
            sources.append(extra)
    source_text = "  •  ".join(sources[:2])
    src_font = _font("Montserrat-Bold.ttf", DATE_FONT_SIZE - 2)
    src_w = int(draw.textlength(source_text, font=src_font))
    src_h = DATE_FONT_SIZE - 2

    badge_pad_x = 18
    badge_pad_y = 8
    badge_w = src_w + badge_pad_x * 2
    badge_h = src_h + badge_pad_y * 2
    badge_x = (IMAGE_WIDTH - badge_w) // 2
    badge_y = BOTTOM_TOP + (bottom_bar_h - badge_h) // 2

    # Dark red badge background with rounded feel (rectangle)
    draw.rectangle(
        [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
        fill=(140, 20, 30),   # dark red
        outline=RED,
        width=2,
    )
    # White source text inside badge
    draw.text(
        (badge_x + badge_pad_x, badge_y + badge_pad_y),
        source_text, font=src_font, fill=WHITE,
    )

    # Date bottom-right
    date_str = datetime.now(timezone.utc).strftime("%d %b %Y").upper()
    date_font = _font("Montserrat-Medium.ttf", DATE_FONT_SIZE)
    date_w = int(draw.textlength(date_str, font=date_font))
    date_x = IMAGE_WIDTH - MARGIN_RIGHT - date_w
    date_y = BOTTOM_TOP + (bottom_bar_h - DATE_FONT_SIZE) // 2
    draw.text((date_x, date_y), date_str, font=date_font, fill=OFF_WHITE)

    return canvas


def _context_line(story: Story) -> str:
    """Extract a short 3–7 word context line from the story summary."""
    summary = story.raw_summary or ""
    if not summary:
        return ""
    words = summary.split()
    if len(words) < 4:
        return ""
    # Take first sentence, cap at 7 words
    first_sentence = summary.split(".")[0].strip()
    words = first_sentence.split()
    if len(words) > 7:
        return " ".join(words[:7]) + "..."
    return first_sentence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _smart_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize and center-crop image to exact target dimensions."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top  = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _wrap_text(draw: ImageDraw.Draw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Wrap text to fit within max_width pixels."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines)


def _short_headline(title: str) -> str:
    """
    Rewrite the title into a short, punchy 6–12 word headline.
    Removes source attribution (e.g. '- BBC News', '| Reuters').
    """
    # Strip source attribution appended by NewsAPI
    for sep in [" - ", " | ", " — ", " – "]:
        if sep in title:
            title = title[:title.rfind(sep)].strip()

    words = title.split()
    if len(words) <= 12:
        return title.upper()

    # Truncate to 10 words at a natural break
    short = " ".join(words[:10])
    return short.upper() + "..."


def _keywords(title: str, broad: bool = False) -> str:
    """Extract 2–3 search keywords from title."""
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
