# Facebook News Agent

An automated system that discovers worldwide news, checks independent-source evidence,
scores editorial importance, creates a Facebook post and image card, and routes verified
stories through a scheduled publishing queue. Strong single-source stories are preserved
as drafts for review and are blocked from automatic publishing.

---

## Prerequisites

- Python 3.11 or newer
- A [NewsAPI](https://newsapi.org) API key (free tier: 100 requests/day)
- Local Ollama with the configured model, or an OpenAI API key
- Facebook Page credentials when publishing is enabled
- A Pexels API key when image cards are enabled

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
# Fetch, verify, build posts and image cards, then fill the queue
python agent.py --fetch --image

# Preview the current publish queue without writing files or posting
python agent.py --publish --dry-run

# Publish only independently verified entries that pass queue rules
python agent.py --publish
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

### Verification and scoring

Verification and editorial importance are separate decisions:

- `VERIFIED` requires matching reports from at least two independent Tier 1–3 domains.
- Reports from the same publisher's subdomains count as one source.
- Near-identical syndicated copies do not count as independent confirmation.
- Material conflicts in outcomes or casualty figures reject the story.
- Selected stories receive a deeper check using public article text when accessible.
- A powerful single-source story keeps its full editorial score and becomes a `DRAFT`.
  It cannot enter the automatic Facebook publishing path.

### JSON structure

```json
{
  "title": "...",
  "source_name": "BBC News",
  "source_url": "https://...",
  "published_at": "2026-08-31T12:00:00+00:00",
  "news_summary": "...",
  "verification_status": "VERIFIED",
  "verification_score": 86,
  "verification_reason": "Confirmed by 2 independent reliable domains...",
  "verification_evidence": [{"domain": "bbc.com", "matched": true}],
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
│   ├── claim_matcher.py  # Compares core facts and contradictions
│   ├── verifier.py       # Independent-source evidence gate
│   └── generator.py      # LLM-based Facebook post generation
├── news/article_extractor.py # Reads public article text for deep verification
├── output/
│   └── draft_writer.py   # Writes JSON + Markdown draft files
├── tests/                # Unit tests
├── drafts/               # Generated drafts (gitignored)
├── seen_stories.json     # Duplicate detection log (gitignored)
├── .env                  # Your API keys (gitignored)
└── .env.example          # Template — safe to commit
```

---

## Current safety boundary

Source agreement is deterministic and auditable, but it is not a professional fact-checking
service. Image relevance remains a separate improvement area: current stock or generated
images are checked for visual quality, not full factual identity matching.
