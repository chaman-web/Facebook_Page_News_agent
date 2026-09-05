"""
image/maker.py — Step 2: Professional Facebook news post image generator.

Design: Full-bleed photo with gradient overlays, editorial style.
Canvas: 1200 × 1500 px (4:5 mobile-first)

Fallback chain:
  Pexels (keyword) → Pexels (broad) → Pollinations.ai → None (text-only post)
"""

from __future__ import annotations

import logging
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

# ── Brand colors ─────────────────────────────────────────────────────────────
NAVY         = (8,   16,  40)
NAVY_LIGHT   = (18,  32,  72)
RED          = (210, 30,  45)
RED_DARK     = (140, 18,  28)
WHITE        = (255, 255, 255)
OFF_WHITE    = (220, 225, 235)
GOLD         = (255, 200, 60)   # accent for divider lines

# ── Layout zones (px from top) ────────────────────────────────────────────────
#   0        → PHOTO_BOT  : full-bleed photo
#   0        → 380        : top dark gradient overlay  (headline sits here)
#   PHOTO_BOT→ IMAGE_HEIGHT: solid dark panel (context + branding)
PHOTO_BOT    = 1170   # photo ends, dark panel begins

# ── Typography ───────────────────────────────────────────────────────────────
LABEL_SIZE      = 28
HEADLINE_SIZE   = 80
HEADLINE_MIN    = 58
CONTEXT_SIZE    = 32
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
}

# ── Category accent colors ────────────────────────────────────────────────────
CATEGORY_COLORS = {
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
}


# ── Public interface ──────────────────────────────────────────────────────────

def create_news_image(story: Story) -> Optional[Path]:
    IMAGES_DIR = Path("images")
    IMAGES_DIR.mkdir(exist_ok=True)

    photo = _fetch_photo(story)
    if photo is None:
        logger.error("All image sources failed for: %s", story.title)
        return None

    image = _compose(photo, story)

    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in story.title[:45])
    out_path = IMAGES_DIR / f"{safe}.jpg"
    image.save(out_path, "JPEG", quality=93, optimize=True)
    logger.info("Image saved: %s", out_path)
    return out_path


# ── Photo fetching ────────────────────────────────────────────────────────────

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

def _compose(photo: Image.Image, story: Story) -> Image.Image:
    category = (getattr(story, "category", "breaking") or "breaking").lower()
    accent   = CATEGORY_COLORS.get(category, RED)
    label    = CATEGORY_LABELS.get(category, "WORLD NEWS")

    # ── 1. Full-bleed photo as base ───────────────────────────────────────────
    canvas = _smart_crop(photo, IMAGE_WIDTH, IMAGE_HEIGHT).copy()
    draw   = ImageDraw.Draw(canvas, "RGBA")

    # ── 2. Top gradient overlay (dark navy → transparent, top 38%) ────────────
    _gradient_rect(draw, 0, 0, IMAGE_WIDTH, int(IMAGE_HEIGHT * 0.42),
                   top_color=(*NAVY, 230), bottom_color=(*NAVY, 0))

    # ── 3. Bottom gradient overlay (transparent → dark navy, bottom 42%) ──────
    fade_start = int(IMAGE_HEIGHT * 0.58)
    _gradient_rect(draw, 0, fade_start, IMAGE_WIDTH, IMAGE_HEIGHT,
                   top_color=(*NAVY, 0), bottom_color=(*NAVY, 255))

    # ── 4. Heavy bottom gradient (no solid panel — photo bleeds to edge) ────────
    # Already handled by the bottom gradient drawn in step 3 above
    # Just ensure bottom 200px is very dark for footer readability
    _gradient_rect(draw, 0, IMAGE_HEIGHT - 280, IMAGE_WIDTH, IMAGE_HEIGHT,
                   top_color=(*NAVY, 0), bottom_color=(*NAVY, 210))

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

    bx1 = ML
    by1 = MT
    bx2 = bx1 + lbl_w + lbl_px * 2
    by2 = by1 + LABEL_SIZE + lbl_py * 2

    # Badge fill
    draw.rectangle([(bx1, by1), (bx2, by2)], fill=accent)
    # Left bold accent stripe on badge
    draw.rectangle([(bx1, by1), (bx1 + 6, by2)], fill=WHITE)
    # Label text
    draw.text((bx1 + lbl_px + 8, by1 + lbl_py), label, font=lbl_font, fill=WHITE)

    label_bottom = by2

    # ── 9. Main headline ──────────────────────────────────────────────────────
    headline  = _short_headline(story.title)
    max_w     = IMAGE_WIDTH - ML - MR
    hl_size   = HEADLINE_SIZE
    hl_font   = _font("Montserrat-ExtraBold.ttf", hl_size)
    wrapped   = _wrap_text(draw, headline, hl_font, max_w)
    lines     = wrapped.splitlines()

    while (len(lines) > 3 or len(lines) * int(hl_size * 1.2) > 290) and hl_size > HEADLINE_MIN:
        hl_size -= 4
        hl_font  = _font("Montserrat-ExtraBold.ttf", hl_size)
        wrapped  = _wrap_text(draw, headline, hl_font, max_w)
        lines    = wrapped.splitlines()

    line_h = int(hl_size * 1.2)
    hl_y   = label_bottom + 22

    for line in lines[:3]:
        # Multi-layer shadow for depth
        for ox, oy in [(3, 3), (2, 2), (1, 1)]:
            draw.text((ML + ox, hl_y + oy), line, font=hl_font, fill=(0, 0, 0))
        draw.text((ML, hl_y), line, font=hl_font, fill=WHITE)
        hl_y += line_h

    # ── 10. Thin gold rule under headline ─────────────────────────────────────
    rule_y = hl_y + 12
    draw.rectangle([(ML, rule_y), (ML + 120, rule_y + 3)], fill=accent)

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
    # Layout: Logo | vertical divider | source pill  ···  date + wordmark
    # All sitting in the bottom 200px of the canvas

    FOOTER_MID = IMAGE_HEIGHT - 95   # vertical center of footer row

    # ── Logo ─────────────────────────────────────────────────────────────────
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
    pri_name = sources[0].upper()
    pri_w    = int(draw.textlength(pri_name, font=pri_font))

    # Accent dot
    dot_r  = 6
    dot_cx = src_x + dot_r
    dot_cy = sy + pri_h // 2
    draw.ellipse([(dot_cx - dot_r, dot_cy - dot_r),
                  (dot_cx + dot_r, dot_cy + dot_r)], fill=accent)

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


def _context_line(story: Story) -> str:
    summary = story.raw_summary or ""
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
