"""Fetch a source page and extract bounded article text and Open Graph metadata."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

MAX_HTML_CHARS = 2_000_000
MAX_ARTICLE_CHARS = 8_000


@dataclass
class ArticleContent:
    text: str = ""
    description: str = ""
    image_url: str | None = None


class _ArticleParser(HTMLParser):
    _ignored = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignore_depth = 0
        self._article_depth = 0
        self._in_p = False
        self._paragraph_parts: list[str] = []
        self.article_paragraphs: list[str] = []
        self.all_paragraphs: list[str] = []
        self.description = ""
        self.image_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {str(k).lower(): (v or "") for k, v in attrs}
        if tag in self._ignored:
            self._ignore_depth += 1
        if tag in {"article", "main"}:
            self._article_depth += 1
        if tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").lower()
            value = attr.get("content", "").strip()
            if key in {"og:description", "twitter:description", "description"} and not self.description:
                self.description = value
            if key in {"og:image", "twitter:image", "twitter:image:src"} and not self.image_url:
                self.image_url = value
        if tag == "p" and self._ignore_depth == 0:
            self._in_p = True
            self._paragraph_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "p" and self._in_p:
            paragraph = " ".join("".join(self._paragraph_parts).split())
            if len(paragraph) >= 40:
                self.all_paragraphs.append(paragraph)
                if self._article_depth:
                    self.article_paragraphs.append(paragraph)
            self._in_p = False
            self._paragraph_parts = []
        if tag in {"article", "main"} and self._article_depth:
            self._article_depth -= 1
        if tag in self._ignored and self._ignore_depth:
            self._ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_p and self._ignore_depth == 0:
            self._paragraph_parts.append(data)


def fetch_article(url: str, timeout: int = 8) -> ArticleContent:
    """Return useful article text when the publisher page is publicly accessible."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ArticleContent()

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; GlobalPulseNews/1.0; "
                    "+https://www.facebook.com/)"
                )
            },
            timeout=timeout,
        )
        response.raise_for_status()
        if "html" not in response.headers.get("content-type", "text/html").lower():
            return ArticleContent()

        parser = _ArticleParser()
        parser.feed(response.text[:MAX_HTML_CHARS])
        paragraphs = parser.article_paragraphs
        if len(" ".join(paragraphs)) < 200:
            paragraphs = parser.all_paragraphs

        seen: set[str] = set()
        unique: list[str] = []
        for paragraph in paragraphs:
            key = paragraph.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(paragraph)

        text = "\n\n".join(unique)[:MAX_ARTICLE_CHARS].strip()
        return ArticleContent(
            text=text,
            description=parser.description or "",
            image_url=parser.image_url,
        )
    except Exception as exc:
        logger.debug("Full article unavailable for %s: %s", parsed.netloc, exc)
        return ArticleContent()
