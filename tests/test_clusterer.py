from datetime import datetime, timezone

from models import Story
from pipeline.clusterer import _build_clusters


def _story(title: str, summary: str, url: str) -> Story:
    return Story(
        title=title,
        source_name="Test News",
        source_url=url,
        published_at=datetime.now(timezone.utc),
        raw_summary=summary,
    )


def test_paraphrased_reports_cluster_using_headline_and_lead_context():
    first = _story(
        "Russia opens road bridge to North Korea",
        "The first road bridge connects Russia and North Korea across the Tumen River.",
        "https://npr.org/a",
    )
    second = _story(
        "Moscow and Pyongyang inaugurate Tumen crossing",
        "The road bridge connects North Korea and Russia across the Tumen River.",
        "https://bbc.com/b",
    )
    assert len(_build_clusters([first, second])) == 1


def test_shared_template_does_not_merge_unrelated_events():
    first = _story(
        "Government opens road bridge in northern province",
        "A local road bridge opened after two years of construction.",
        "https://npr.org/a",
    )
    second = _story(
        "Company opens technology center in southern city",
        "A software company opened a new research office for engineers.",
        "https://bbc.com/b",
    )
    assert len(_build_clusters([first, second])) == 2
