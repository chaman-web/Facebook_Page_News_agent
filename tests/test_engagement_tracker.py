from pathlib import Path
from unittest.mock import Mock, patch

from facebook.engagement_tracker import fetch_post_metrics


def test_fetch_post_metrics_parses_facebook_summaries():
    response = Mock()
    response.json.return_value = {
        "created_time": "2026-09-08T08:44:37+0000",
        "shares": {"count": 7},
        "reactions": {"summary": {"total_count": 41}},
        "comments": {"summary": {"total_count": 9}},
    }
    response.raise_for_status.return_value = None
    with patch("facebook.engagement_tracker.requests.get", return_value=response):
        result = fetch_post_metrics("page_post", "token")

    assert result["reactions"] == 41
    assert result["comments"] == 9
    assert result["shares"] == 7
