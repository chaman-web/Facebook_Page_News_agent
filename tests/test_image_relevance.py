from datetime import datetime, timezone
from unittest.mock import patch

from PIL import Image

from models import Story
from image.maker import (
    _context_line,
    _fetch_article_photo,
    _image_description_matches,
    _smart_crop,
    _source_display_name,
    create_fallback_card,
)


def test_stock_metadata_must_overlap_query():
    assert _image_description_matches("Canada tariffs", "Canadian flag outside parliament")
    assert not _image_description_matches("Canada tariffs", "Laptop on an office desk")


def test_article_photo_is_preferred_when_available():
    story = Story(
        title="Canada introduces new tariffs",
        source_name="BBC News",
        source_url="https://bbc.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="Canada introduced new tariffs.",
        article_image_url="https://example.com/authentic.jpg",
    )
    expected = Image.new("RGB", (1200, 800))
    with patch("image.maker._download", return_value=expected) as download:
        result = _fetch_article_photo(story)
    assert result is expected
    download.assert_called_once_with(story.article_image_url)


def test_local_fallback_always_creates_an_image_card(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path)
    story = Story(
        title="Verified story needs a fallback card",
        source_name="BBC News",
        source_url="https://bbc.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="A verified story remains publishable when external image services fail.",
        category="world",
    )
    path = create_fallback_card(story)
    assert path.exists()
    assert Image.open(path).size == (1200, 1500)


def test_smart_crop_keeps_off_center_subject_visible():
    import numpy as np
    image = Image.new("RGB", (2400, 1500), "#202020")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image)
    draw.rectangle((1850, 350, 2250, 1150), fill="#ef233c", outline="white", width=20)
    cropped = _smart_crop(image, 1200, 1500)
    pixels = np.asarray(cropped.resize((120, 150)))
    assert ((pixels[:, :, 0] > 180) & (pixels[:, :, 0] > pixels[:, :, 1] * 1.5)).any()


def test_card_context_uses_distinct_high_value_detail():
    story = Story(
        title="UK imposes sanctions on West Bank settlements",
        source_name="NYT > World",
        source_url="https://nytimes.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary=(
            "The UK imposes sanctions on West Bank settlements. "
            "The package is expected to include a trade ban on goods produced there."
        ),
    )
    context = _context_line(story)
    assert "trade ban" in context.lower()
    assert context.lower() != story.title.lower()


def test_source_display_name_uses_publisher_domain():
    assert _source_display_name("NYT > World", "https://www.nytimes.com/world/story") == "New York Times"
    assert _source_display_name("World", "https://www.washingtonpost.com/world/story") == "Washington Post"
