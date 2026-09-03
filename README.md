# Facebook News Agent

An automated system that discovers recent worldwide news, verifies it across multiple
sources, generates an original Facebook post, and saves it as a draft for human review.

**Phase 1** — News discovery → verification → draft generation. Nothing is published automatically.

---

## Prerequisites

- Python 3.11 or newer
- A [NewsAPI](https://newsapi.org) API key (free tier: 100 requests/day)
- An [OpenAI](https://platform.openai.com) API key (GPT-4o)

---

## Setup

```bash
# 1. Clone the repository
git clone <your-repo-url>
cd facebook-news-agent

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure API keys
cp .env.example .env
# Open .env and fill in your NEWSAPI_KEY and OPENAI_API_KEY
```

---

## Running the agent

```bash
# Normal run — fetches news, verifies, generates post, saves draft
python agent.py

# Dry run — runs the full pipeline but writes no files (good for testing)
python agent.py --dry-run
```

---

## Reading a draft

Drafts are saved in the `drafts/` directory with two files per run:

```
drafts/
├── 2026-08-31_18-00_global-leaders-agree-on-trade.json   ← machine-readable
└── 2026-08-31_18-00_global-leaders-agree-on-trade.md     ← human-readable preview
```

### Draft statuses

| Status | Meaning |
|---|---|
| `READY_FOR_REVIEW` | Verified by 2+ sources. Ready for a human to approve and publish. |
| `DRAFT` | Only 1 source found. Review carefully before publishing. |
| `REJECTED` | Failed quality or safety checks. Reason is recorded in the file. |

### JSON structure

```json
{
  "title": "...",
  "source_name": "BBC News",
  "source_url": "https://...",
  "published_at": "2026-08-31T12:00:00+00:00",
  "news_summary": "...",
  "verification_status": "VERIFIED",
  "corroborating_sources": [{"name": "Reuters", "url": "..."}],
  "post_content": "...",
  "hashtags": ["#WorldNews", "..."],
  "draft_status": "READY_FOR_REVIEW",
  "rejection_reason": null,
  "generated_at": "2026-08-31T18:00:00+00:00"
}
```

---

## Running tests

```bash
pip install pytest
pytest tests/ -v
```

---

## Project structure

```
facebook-news-agent/
├── agent.py              # Entry point — orchestrates the pipeline
├── config.py             # Loads .env, exposes typed config constants
├── models.py             # Story dataclass, status enums, exceptions
├── news/
│   └── fetcher.py        # Fetches news from NewsAPI + RSS fallback
├── pipeline/
│   ├── selector.py       # Picks the best story candidate
│   ├── deduplicator.py   # Detects already-processed stories
│   ├── verifier.py       # Cross-source verification
│   └── generator.py      # LLM-based Facebook post generation
├── output/
│   └── draft_writer.py   # Writes JSON + Markdown draft files
├── tests/                # Unit tests
├── drafts/               # Generated drafts (gitignored)
├── seen_stories.json     # Duplicate detection log (gitignored)
├── .env                  # Your API keys (gitignored)
└── .env.example          # Template — safe to commit
```

---

## Roadmap

- **Phase 2** — Better news discovery, multiple categories, story scoring
- **Phase 3** — Facebook Page publishing via Graph API
- **Phase 4** — AI image generation for posts
- **Phase 5** — Short video / Reel generation
- **Phase 6** — Automatic scheduling
- **Phase 7** — Facebook analytics and content optimisation
