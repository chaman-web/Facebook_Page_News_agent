from datetime import datetime, timezone
from unittest.mock import patch

from PIL import Image

from models import Story
from image.maker import (
    _context_line,
    _fallback_background,
    _fetch_article_photo,
    _find_impact_words,
    _headline_display_case,
    _apply_image_provenance,
    _image_description_matches,
    _keywords,
    _synthetic_image_allowed,
    _smart_crop,
    _source_display_name,
    create_fallback_card,
)


def test_stock_metadata_must_overlap_query():
    assert _image_description_matches("Canada tariffs", "Canadian flag outside parliament")
    assert not _image_description_matches("Canada tariffs", "Laptop on an office desk")
    assert not _image_description_matches("u.s", "Woman standing beside scooter")


def test_visual_query_uses_story_subject_instead_of_country_abbreviation():
    title = "U.S. military says it destroyed 5 Iranian oil tankers"
    assert _keywords(title) == "oil tanker ship ocean"
    assert _keywords(title, broad=True) == "oil tanker"


def test_headline_display_case_preserves_news_acronyms():
    assert (
        _headline_display_case("UK AND US AGREE ON AI SAFETY PLAN")
        == "UK and US Agree on AI Safety Plan"
    )


def test_headline_emphasis_selects_two_high_impact_words():
    assert _find_impact_words("Earthquake Kills 12 People in Turkey") == [
        "Kills", "Earthquake",
    ]


def test_headline_emphasis_uses_specific_anchors_without_impact_terms():
    assert _find_impact_words("Apple Unveils Foldable iPhone") == [
        "Foldable", "iPhone",
    ]


def test_article_photo_is_preferred_when_available():
    story = Story(
        title="Canada introduces new tariffs",
        source_name="BBC News",
        source_url="https://bbc.com/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="Canada introduced new tariffs.",
        article_image_url="https://example.com/authentic.jpg",
        article_image_reuse_permitted=True,
    )
    expected = Image.new("RGB", (1200, 800))
    with patch("image.maker._download", return_value=expected) as download:
        result = _fetch_article_photo(story)
    assert result is expected
    download.assert_called_once_with(story.article_image_url)


def test_article_photo_requires_recorded_reuse_permission():
    story = Story(
        title="Publisher image has unknown rights",
        source_name="News Publisher",
        source_url="https://publisher.example/story",
        published_at=datetime.now(timezone.utc),
        raw_summary="A verified report.",
        article_image_url="https://publisher.example/photo.jpg",
    )
    with patch("image.maker._download") as download:
        assert _fetch_article_photo(story) is None
    download.assert_not_called()


def test_synthetic_image_provenance_is_attached_to_story():
    story = Story(
        title="Synthetic illustration test",
        source_name="Reuters",
        source_url="https://reuters.com/test",
        published_at=datetime.now(timezone.utc),
        raw_summary="A verified report.",
    )
    image = Image.new("RGB", (1200, 1500))
    image.info.update({
        "image_provenance": "pollinations_ai",
        "image_credit": "Pollinations.ai",
        "image_is_synthetic": True,
    })

    _apply_image_provenance(story, image)

    assert story.image_provenance == "pollinations_ai"
    assert story.image_is_synthetic is True


def test_synthetic_images_are_skipped_for_sensitive_stories():
    sensitive = Story(
        title="Earthquake leaves dozens dead",
        source_name="Reuters",
        source_url="https://reuters.com/earthquake",
        published_at=datetime.now(timezone.utc),
        raw_summary="Emergency crews responded after the disaster.",
        category="world",
    )
    safe = Story(
        title="New processor improves laptop battery life",
        source_name="Reuters",
        source_url="https://reuters.com/technology",
        published_at=datetime.now(timezone.utc),
        raw_summary="The company announced its latest processor.",
        category="technology",
    )

    assert _synthetic_image_allowed(sensitive) is False
    assert _synthetic_image_allowed(safe) is True


def test_breaking_category_skips_synthetic_image_even_without_sensitive_words():
    story = Story(
        title="Government announces urgent national update",
        source_name="Reuters",
        source_url="https://reuters.com/breaking",
        published_at=datetime.now(timezone.utc),
        raw_summary="Officials released a new statement.",
        category="breaking",
    )
    assert _synthetic_image_allowed(story) is False


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
    with patch("image.maker._smart_crop", side_effect=RuntimeError("NumPy unavailable")):
        path = create_fallback_card(story)
    assert path.exists()
    assert Image.open(path).size == (1200, 1500)


def test_fallback_background_is_deterministic_but_varies_by_story():
    published = datetime(2026, 9, 9, tzinfo=timezone.utc)
    fuel_story = Story(
        title="Pakistan raises petrol prices",
        source_name="Dawn",
        source_url="https://dawn.com/fuel",
        published_at=published,
        raw_summary="New petrol prices affect transport costs and the economy.",
        category="breaking",
    )
    trade_story = Story(
        title="Canada introduces new trade tariffs",
        source_name="Reuters",
        source_url="https://reuters.com/trade",
        published_at=published,
        raw_summary="The tariff decision changes trade costs for businesses.",
        category="breaking",
    )

    fuel_first = _fallback_background(fuel_story)
    fuel_second = _fallback_background(fuel_story)
    trade = _fallback_background(trade_story)

    assert fuel_first.tobytes() == fuel_second.tobytes()
    assert fuel_first.tobytes() != trade.tobytes()


def test_failed_photo_is_not_reused_for_later_empty_searches(tmp_path):
    story = Story(
        title="Verified story with no suitable external image",
        source_name="BBC News",
        source_url="https://bbc.com/no-image",
        published_at=datetime.now(timezone.utc),
        raw_summary="The story is verified but its available photograph is unsuitable.",
        category="world",
    )
    rejected_photo = Image.new("RGB", (1200, 1500), "#222222")
    composed = Image.new("RGB", (1200, 1500), "#111111")
    fallback_path = tmp_path / "fallback.jpg"

    with (
        patch("image.maker._fetch_photo", return_value=rejected_photo),
        patch("image.maker._fetch_photo_by_keyword", return_value=None),
        patch("image.maker._compose", return_value=composed) as compose,
        patch("image.maker._mobile_visibility_check", return_value={"photo_blur": "too soft"}),
        patch("image.maker.create_fallback_card", return_value=fallback_path),
    ):
        from image.maker import create_news_image
        result = create_news_image(story)

    assert result == fallback_path
    assert compose.call_count == 1


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
