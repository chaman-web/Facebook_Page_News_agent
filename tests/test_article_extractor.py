"""Tests for bounded public article extraction."""

from unittest.mock import Mock, patch

from news.article_extractor import fetch_article


def test_extracts_article_text_and_open_graph_image():
    response = Mock()
    response.headers = {"content-type": "text/html; charset=utf-8"}
    response.text = """
        <html><head>
          <meta property="og:description" content="A verified description.">
          <meta property="og:image" content="https://example.com/news-photo.jpg">
        </head><body>
          <nav><p>This navigation paragraph must be ignored completely.</p></nav>
          <article>
            <p>Officials confirmed the central facts of this developing news event today.</p>
            <p>A second substantial paragraph provides location, timing, and useful context.</p>
            <p>A third substantial paragraph explains the response from relevant authorities.</p>
          </article>
        </body></html>
    """
    response.raise_for_status.return_value = None

    with patch("news.article_extractor.requests.get", return_value=response):
        article = fetch_article("https://example.com/story")

    assert "central facts" in article.text
    assert "navigation" not in article.text
    assert article.description == "A verified description."
    assert article.image_url == "https://example.com/news-photo.jpg"


def test_invalid_url_does_not_make_network_request():
    with patch("news.article_extractor.requests.get") as request:
        article = fetch_article("not-a-url")

    request.assert_not_called()
    assert article.text == ""
