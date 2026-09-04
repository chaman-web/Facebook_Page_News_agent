"""
config.py — Load and validate environment configuration.

Reads from .env (via python-dotenv) and exposes typed constants.
Fails fast with a clear error message if required values are missing.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    """Return the value of an environment variable or exit with a clear error."""
    value = os.getenv(key, "").strip()
    if not value:
        print(f"[ERROR] Required environment variable '{key}' is not set.")
        print(f"        Copy .env.example to .env and fill in your API keys.")
        sys.exit(1)
    return value


def _optional(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


# ---------------------------------------------------------------------------
# LLM backend selection
# ---------------------------------------------------------------------------

# "ollama" (default, free, local) or "openai" (paid, cloud)
LLM_BACKEND: str = _optional("LLM_BACKEND", "ollama")

# ---------------------------------------------------------------------------
# Ollama settings (used when LLM_BACKEND=ollama)
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL: str = _optional("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL: str = _optional("OLLAMA_MODEL", "llama3.2")

# ---------------------------------------------------------------------------
# OpenAI settings (used when LLM_BACKEND=openai)
# ---------------------------------------------------------------------------

OPENAI_MODEL: str = _optional("OPENAI_MODEL", "gpt-4o")

# Only required when LLM_BACKEND=openai
OPENAI_API_KEY: str = ""
if LLM_BACKEND == "openai":
    OPENAI_API_KEY = _require("OPENAI_API_KEY")
else:
    OPENAI_API_KEY = _optional("OPENAI_API_KEY", "")

# ---------------------------------------------------------------------------
# NewsAPI (always required for news fetching)
# ---------------------------------------------------------------------------

NEWSAPI_KEY: str = _require("NEWSAPI_KEY")

# NewsAPI endpoints
NEWSAPI_TOP_HEADLINES_URL = "https://newsapi.org/v2/top-headlines"
NEWSAPI_EVERYTHING_URL = "https://newsapi.org/v2/everything"

# ---------------------------------------------------------------------------
# RSS fallback feeds (free, no API key needed)
# ---------------------------------------------------------------------------

RSS_FALLBACK_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",          # BBC World
    "https://feeds.reuters.com/reuters/topNews",            # Reuters
    "https://rss.app/feeds/tvAoqbQrAjGnvHZs.xml",          # AP News
    "https://www.dawn.com/feeds/home",                      # Dawn (Pakistan)
    "https://www.thehindu.com/news/international/?service=rss",  # The Hindu
    "https://www.aljazeera.com/xml/rss/all.xml",            # Al Jazeera
]

# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------

NEWS_MAX_AGE_HOURS: int = int(_optional("NEWS_MAX_AGE_HOURS", "48"))
NEWS_MIN_SOURCES: int = int(_optional("NEWS_MIN_SOURCES", "2"))

# Paths
SEEN_STORIES_PATH = "seen_stories.json"
DRAFTS_DIR = "drafts"

# Duplicate detection threshold (0.0 – 1.0)
DUPLICATE_TITLE_THRESHOLD = 0.85

# ---------------------------------------------------------------------------
# Facebook publishing (Phase 3 — only needed when using --publish)
# ---------------------------------------------------------------------------

FACEBOOK_PAGE_ID: str = _optional("FACEBOOK_PAGE_ID", "")
FACEBOOK_PAGE_TOKEN: str = _optional("FACEBOOK_PAGE_TOKEN", "")
FACEBOOK_APP_ID: str = _optional("FACEBOOK_APP_ID", "")
FACEBOOK_APP_SECRET: str = _optional("FACEBOOK_APP_SECRET", "")

# ---------------------------------------------------------------------------
# Pexels image search (Step 2 — only needed when using --image)
# ---------------------------------------------------------------------------

PEXELS_API_KEY: str = _optional("PEXELS_API_KEY", "")
