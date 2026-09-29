"""Tests for the isolated, offline Reel generator."""

import json
from pathlib import Path

from PIL import Image

from reel_generator import _load_queue_entry
from reels.generator import ReelGenerator, ReelInput, _font, _wrap_pixels


def _item(image_path: Path | None = None) -> ReelInput:
    return ReelInput(
        title="Verified infrastructure update announced",
        source_name="Reuters",
        source_url="https://example.com/story",
        category="world",
        post_content="Officials confirmed the update. It affects transport across the region.",
        card_description="Transport services will change across the region.",
        image_path=str(image_path) if image_path else None,
    )


def test_frames_and_manifest_are_written_only_to_reel_output(tmp_path):
    source = tmp_path / "source.jpg"
    Image.new("RGB", (400, 300), "#345678").save(source)
    output = tmp_path / "reels"

    result = ReelGenerator(output, width=360, height=640).generate(
        _item(source), render_video=False
    )

    assert result.video_path is None
    assert len(result.frame_paths) == 4
    assert all(Path(path).is_file() for path in result.frame_paths)
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["publishing_enabled"] is False
    assert manifest["input"]["title"] == _item().title
    assert source.read_bytes().startswith(b"\xff\xd8")


def test_queue_reader_does_not_modify_queue(tmp_path):
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"entries": [{
        "title": "Latest story",
        "source_name": "AP",
        "source_url": "https://example.com/latest",
        "post_content": "Confirmed details.",
    }]}), encoding="utf-8")
    before = queue.read_bytes()

    item = _load_queue_entry(queue, -1)

    assert item.title == "Latest story"
    assert queue.read_bytes() == before


def test_video_assembly_uses_ffmpeg_without_publishing(monkeypatch, tmp_path):
    calls = []

    class Completed:
        returncode = 0
        stderr = ""

    monkeypatch.setattr("reels.generator.shutil.which", lambda _: "ffmpeg")
    monkeypatch.setattr(
        "reels.generator.subprocess.run",
        lambda command, **kwargs: calls.append(command) or Completed(),
    )
    result = ReelGenerator(tmp_path / "out", width=360, height=640).generate(_item())

    assert result.video_path is not None
    assert len(calls) == 1
    assert "ffmpeg" in calls[0][0]
    assert all("publish" not in part.lower() for part in calls[0])
    assert calls[0][calls[0].index("-t") + 1] == "12"


def test_headline_wrap_respects_render_width():
    image = Image.new("RGB", (360, 640))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    font = _font(32)
    lines = _wrap_pixels(
        draw,
        "Warning more homes will be uninsurable due to flood risk",
        font,
        300,
    )
    assert lines
    assert all(draw.textbbox((0, 0), line, font=font)[2] <= 300 for line in lines)
