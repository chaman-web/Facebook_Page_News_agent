# Design — Facebook News Agent (Phase 1)

## Architecture Overview

Phase 1 is a single Python script (`agent.py`) driven by a small pipeline of loosely coupled
steps. Each step is a plain function (or small class) so any step can be replaced or extended
in a later phase without touching the others.

```
┌─────────────────────────────────────────────────────────┐
│                        agent.py                         │
│                                                         │
│  1. fetch_news()          ← News source adapter         │
│       │                                                  │
│  2. select_story()        ← Pick best candidate         │
│       │                                                  │
│  3. check_duplicate()     ← seen_stories.json           │
│       │                                                  │
│  4. verify_story()        ← Cross-source check          │
│       │                                                  │
│  5. generate_post()       ← LLM content generation      │
│       │                                                  │
│  6. save_draft()          ← drafts/ directory           │
└─────────────────────────────────────────────────────────┘
```

Each step receives a `Story` data object and returns an updated version of it (or raises
a `StoryRejected` exception to halt the pipeline with a REJECTED draft).

---

## Data Model

### Story (internal object, passed through the pipeline)

```python
@dataclass
class Story:
    # Discovery
    title: str
    source_name: str
    source_url: str
    published_at: datetime
    raw_summary: str

    # Verification
    corroborating_sources: list[dict]   # [{name, url}]
    verification_status: str            # VERIFIED | UNVERIFIED | SPECULATIVE

    # Generation
    post_content: str | None
    hashtags: list[str]

    # Draft
    draft_status: str                   # DRAFT | READY_FOR_REVIEW | REJECTED
    rejection_reason: str | None
    generated_at: datetime | None
```

### Draft file (JSON)

```json
{
  "title": "...",
  "source_name": "...",
  "source_url": "...",
  "published_at": "2026-08-31T12:00:00Z",
  "news_summary": "...",
  "verification_status": "VERIFIED",
  "post_content": "...",
  "hashtags": ["#WorldNews", "..."],
  "draft_status": "READY_FOR_REVIEW",
  "rejection_reason": null,
  "generated_at": "2026-08-31T18:00:00Z"
}
```

A matching `.md` file is generated as a human-readable preview.

---

## Module Layout

```
facebook-news-agent/
├── agent.py              # Entry point; orchestrates the pipeline
├── config.py             # Loads .env, exposes typed config constants
├── models.py             # Story dataclass and status enums
├── news/
│   ├── __init__.py
│   └── fetcher.py        # fetch_news() — calls NewsAPI or RSS
├── pipeline/
│   ├── __init__.py
│   ├── selector.py       # select_story()
│   ├── deduplicator.py   # check_duplicate(), update seen_stories.json
│   ├── verifier.py       # verify_story() — cross-source check
│   └── generator.py      # generate_post() — LLM call
├── output/
│   ├── __init__.py
│   └── draft_writer.py   # save_draft() — writes JSON + MD
├── drafts/               # Generated draft files (gitignored)
├── seen_stories.json     # Duplicate detection log (gitignored)
├── .env                  # Secrets (gitignored)
├── .env.example          # Template with placeholder values
├── requirements.txt
└── README.md
```

---

## Technology Choices

### Language: Python 3.11+

Rationale: mature ecosystem for API clients, HTTP, LLM SDKs, and data processing.
Simple to read and extend.

### News Source: NewsAPI (newsapi.org)

- Free tier: up to 100 requests/day, articles from 80,000+ sources.
- Returns structured JSON with title, source, URL, publishedAt, and description.
- Alternative / fallback: feedparser (RSS) from BBC, Reuters, or AP feeds.
- Phase 2 can add more sources without changing downstream pipeline steps.

### LLM: OpenAI GPT-4o (via `openai` Python SDK)

- Used only in `generator.py` to write the Facebook post from the verified facts.
- The prompt is templated so the model can be swapped (e.g. to Anthropic Claude or a
  local model) by changing one config value.
- The LLM is given only the structured facts extracted from the verified sources — it
  is not given raw article text to paraphrase, reducing hallucination risk.

### Duplicate Detection: JSON file (`seen_stories.json`)

- Stores URL + normalised title of every processed story.
- Title similarity check uses `difflib.SequenceMatcher` (stdlib, no extra dependency).
- Phase 2 can replace this with a vector store or database without changing the interface.

### Draft Storage: Local files (`drafts/`)

- JSON for machine consumption, Markdown for human review.
- No database needed in Phase 1.

### Configuration: python-dotenv

- `.env` holds `NEWSAPI_KEY`, `OPENAI_API_KEY`, and tunable parameters.
- `config.py` reads and validates them at startup, failing fast with a clear error message
  if required values are missing.

### No web framework, no database, no scheduler in Phase 1.

---

## Pipeline Step Details

### 1. fetch_news()
- Calls NewsAPI `/v2/top-headlines` with `language=en` and `pageSize=20`.
- Returns a list of up to 20 Story objects with fields populated from the API response.
- On API failure: logs the error, raises `NewsSourceError`.

### 2. select_story()
- Sorts candidates by `published_at` descending.
- Skips stories with no URL, no title, or with `[Removed]` content (NewsAPI artefact).
- Returns the first acceptable candidate.

### 3. check_duplicate()
- Loads `seen_stories.json` (creates it if absent).
- Checks the story URL against known URLs.
- Checks title similarity ≥ 85% against known titles.
- If duplicate: raises `DuplicateStory`, the pipeline tries the next candidate.

### 4. verify_story()
- Searches NewsAPI for the same story from other sources using the headline keywords.
- Counts distinct source domains reporting the same event.
- Sets `verification_status`:
  - 2+ sources → VERIFIED
  - 1 source → UNVERIFIED
  - 0 additional sources found AND original source is on the unreliable list → SPECULATIVE
- SPECULATIVE → raises `StoryRejected`.
- UNVERIFIED → continues, but final draft_status will be `DRAFT` (not READY_FOR_REVIEW).

### 5. generate_post()
- Builds a structured prompt containing: title, source, publication time, summary, and
  verification status.
- Instructs the LLM to write the post according to the content requirements.
- Instructs the LLM to label unconfirmed claims.
- Parses the LLM response to extract post body and hashtags.
- If LLM call fails: raises `GenerationError`.

### 6. save_draft()
- Sets `draft_status`:
  - VERIFIED → READY_FOR_REVIEW
  - UNVERIFIED → DRAFT
- Writes `drafts/YYYY-MM-DD_HH-MM_<slug>.json`
- Writes `drafts/YYYY-MM-DD_HH-MM_<slug>.md`
- Appends story to `seen_stories.json`.
- Logs the outcome.

---

## Error Handling Strategy

| Condition | Behaviour |
|---|---|
| NewsAPI unreachable | Log error, exit with non-zero code |
| No stories found | Log warning, exit cleanly |
| All candidates are duplicates | Log warning, exit cleanly |
| Story is SPECULATIVE | Save REJECTED draft, continue to exit |
| LLM call fails | Log error, save REJECTED draft, exit |
| Missing API key at startup | Print clear message, exit immediately |

---

## Extensibility Notes

### Adding a new news source (Phase 2)
Create a new file in `news/` implementing the same `fetch_news() -> list[Story]` interface.
Register it in `config.py`. No changes to the pipeline.

### Adding Facebook publishing (Phase 3)
Add `publisher.py` in a new `facebook/` module. Slot it in after `save_draft()` behind a
`--publish` flag. The draft JSON is the contract between Phase 1 and Phase 3.

### Adding image generation (Phase 4)
Add an `image_generator.py` step between `generate_post()` and `save_draft()`.
The Story dataclass gains an `image_path` field.

### Adding scheduling (Phase 6)
Wrap `agent.py` with a cron job or a simple loop with `time.sleep()`. No internal
changes required.

---

## Security Notes

- API keys are read from `.env` only. They are never logged or included in draft files.
- The `.env` file is gitignored.
- `.env.example` contains placeholder values and is safe to commit.
- No user-supplied input is passed to shell commands.
- The system makes no outbound requests except to NewsAPI and OpenAI.
