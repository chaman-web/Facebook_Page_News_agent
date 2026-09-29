"""Generate a silent vertical news Reel without touching publishing state."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_SCENE_SECONDS = 3
DEFAULT_FPS = 30
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FONT_DIR = _PROJECT_ROOT / "assets" / "fonts"


@dataclass(frozen=True)
class ReelInput:
    title: str
    source_name: str
    source_url: str
    category: str = "news"
    post_content: str = ""
    card_headline: str = ""
    card_description: str = ""
    image_path: str | None = None


@dataclass(frozen=True)
class ReelResult:
    output_dir: str
    video_path: str | None
    manifest_path: str
    frame_paths: list[str]


class ReelGenerationError(RuntimeError):
    """Raised when standalone Reel generation cannot complete."""


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    names = (
        ["BarlowCondensed-Bold.ttf", "BarlowCondensed-SemiBold.ttf"]
        if bold
        else ["BarlowCondensed-Regular.ttf", "BarlowCondensed-Medium.ttf"]
    )
    candidates = [*(_FONT_DIR / name for name in names), Path("C:/Windows/Fonts/arialbd.ttf")]
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                pass
    return ImageFont.load_default()


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value[:70] or "news-reel"


def _clean_copy(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"[#*_`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _wrap_pixels(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 6,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
        else:
            current = candidate
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(" ".join(lines).split()) < len(words):
        lines[-1] = lines[-1].rstrip(".") + "..."
    return lines


def _scene_copy(item: ReelInput) -> list[tuple[str, str]]:
    title = _clean_copy(item.card_headline or item.title)
    detail = _clean_copy(item.card_description)
    body = _clean_copy(item.post_content)
    if not detail and body:
        sentences = re.split(r"(?<=[.!?])\s+", body)
        detail = next((s for s in sentences if 35 <= len(s) <= 190), sentences[0])
    detail = detail or "Follow the developing story from verified news sources."
    return [
        (item.category.upper().replace("_", " "), title),
        ("WHAT HAPPENED", detail),
        ("WHY IT MATTERS", body[:220] if body else detail),
        ("GLOBAL PULSE NEWS", f"Source: {item.source_name}"),
    ]


class ReelGenerator:
    """Render a vertical MP4 and audit manifest in a dedicated output directory."""

    def __init__(
        self,
        output_root: Path,
        *,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        scene_seconds: int = DEFAULT_SCENE_SECONDS,
        fps: int = DEFAULT_FPS,
        ffmpeg_binary: str = "ffmpeg",
    ) -> None:
        self.output_root = Path(output_root)
        self.width = width
        self.height = height
        self.scene_seconds = scene_seconds
        self.fps = fps
        self.ffmpeg_binary = ffmpeg_binary

    def generate(self, item: ReelInput, *, render_video: bool = True) -> ReelResult:
        if not item.title.strip() or not item.source_name.strip():
            raise ReelGenerationError("title and source_name are required")

        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_dir = self.output_root / f"{run_id}-{_slug(item.title)}"
        frames_dir = output_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=False)

        frame_paths: list[Path] = []
        scenes = _scene_copy(item)
        for index, (label, copy) in enumerate(scenes, start=1):
            frame_path = frames_dir / f"scene-{index:02d}.jpg"
            self._render_frame(item, label, copy, frame_path, index)
            frame_paths.append(frame_path)

        video_path: Path | None = None
        if render_video:
            video_path = output_dir / "reel.mp4"
            self._assemble_video(frame_paths, video_path)

        manifest_path = output_dir / "manifest.json"
        manifest = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "publishing_enabled": False,
            "input": asdict(item),
            "video": {
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "scene_seconds": self.scene_seconds,
                "duration_seconds": len(scenes) * self.scene_seconds,
                "path": str(video_path) if video_path else None,
            },
            "frames": [str(path) for path in frame_paths],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return ReelResult(
            output_dir=str(output_dir),
            video_path=str(video_path) if video_path else None,
            manifest_path=str(manifest_path),
            frame_paths=[str(path) for path in frame_paths],
        )

    def _background(self, image_path: str | None) -> Image.Image:
        if image_path:
            path = Path(image_path)
            if path.is_file():
                with Image.open(path) as source:
                    image = ImageOps.fit(source.convert("RGB"), (self.width, self.height))
                image = ImageEnhance.Contrast(image).enhance(1.08)
                return image.filter(ImageFilter.GaussianBlur(radius=max(1, self.width // 360)))
        return Image.new("RGB", (self.width, self.height), (10, 18, 38))

    def _render_frame(
        self,
        item: ReelInput,
        label: str,
        copy: str,
        destination: Path,
        scene_number: int,
    ) -> None:
        image = self._background(item.image_path)
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle((0, 0, self.width, self.height), fill=(4, 9, 24, 142))
        draw.rectangle((0, int(self.height * 0.48), self.width, self.height), fill=(4, 9, 24, 220))
        accent = max(10, self.width // 90)
        draw.rectangle((0, 0, accent, self.height), fill=(210, 30, 45, 255))
        image = Image.alpha_composite(image.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(image)

        margin = int(self.width * 0.085)
        label_font = _font(max(18, self.width // 25))
        headline_font = _font(max(30, self.width // 11))
        footer_font = _font(max(16, self.width // 32), bold=False)
        label_y = int(self.height * 0.53)
        draw.text((margin, label_y), label, font=label_font, fill=(244, 52, 66))

        lines = _wrap_pixels(
            draw,
            _clean_copy(copy),
            headline_font,
            self.width - (margin * 2),
        )
        line_height = int(headline_font.size * 1.08)
        y = label_y + int(self.height * 0.055)
        for line in lines:
            draw.text((margin, y), line, font=headline_font, fill=(255, 255, 255))
            y += line_height

        footer_y = self.height - int(self.height * 0.075)
        draw.text((margin, footer_y), "GLOBAL PULSE NEWS", font=footer_font, fill=(225, 230, 240))
        counter = f"{scene_number:02d}/04"
        counter_width = draw.textbbox((0, 0), counter, font=footer_font)[2]
        draw.text((self.width - margin - counter_width, footer_y), counter, font=footer_font, fill=(225, 230, 240))
        image.convert("RGB").save(destination, quality=92, optimize=True)

    def _assemble_video(self, frame_paths: list[Path], destination: Path) -> None:
        ffmpeg = shutil.which(self.ffmpeg_binary)
        if not ffmpeg:
            raise ReelGenerationError(
                "FFmpeg is required for MP4 output. Install it or use --frames-only."
            )
        concat_path = destination.parent / "frames.ffconcat"
        lines = ["ffconcat version 1.0"]
        for path in frame_paths:
            escaped = path.resolve().as_posix().replace("'", "'\\''")
            lines.extend([f"file '{escaped}'", f"duration {self.scene_seconds}"])
        last = frame_paths[-1].resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{last}'")
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-vf", f"fps={self.fps},format=yuv420p",
            "-t", str(len(frame_paths) * self.scene_seconds),
            "-c:v", "libx264", "-movflags", "+faststart", str(destination),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise ReelGenerationError(f"FFmpeg failed: {completed.stderr.strip()}")

