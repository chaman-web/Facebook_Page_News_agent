# Tasks — Facebook News Agent (Phase 1)

## How to use this file

Work through tasks top to bottom. Each task should be completable in one focused session.
Check off tasks as they are done. Do not start a task marked with a blocker until its
dependency is resolved.

Status legend:
- [ ] Not started
- [x] Done

---

## Task Group 1 — Project Setup

### T-01 Initialise project structure
- [ ] Create all directories: `news/`, `pipeline/`, `output/`, `drafts/`
- [ ] Create empty `__init__.py` in each package directory
- [ ] Create `seen_stories.json` with initial content `[]`
- [ ] Add `drafts/` and `seen_stories.json` to `.gitignore`
- [ ] Add `.env` to `.gitignore`

### T-02 Create requirements.txt
- [ ] Pin dependencies:
  - `openai>=1.30.0`
  - `requests>=2.31.0`
  - `python-dotenv>=1.0.0`
  - `feedparser>=6.0.0` (RSS fallback)
- [ ] No other third-party libraries in Phase 1

### T-03 Create .env.example
- [ ] Add placeholder entries:
  - `NEWSAPI_KEY=your_newsapi_key_here`
  - `OPENAI_API_KEY=your_openai_api_key_here`
  - `NEWS_MAX_AGE_HOURS=24`
  - `NEWS_MIN_SOURCES=2`
  - `OPENAI_MODEL=gpt-4o`

### T-04 Create config.py
- [ ] Load `.env` using `python-dotenv`
- [ ] Read and validate `NEWSAPI_KEY` and `OPENAI_API_KEY` — exit with a clear error if missing
- [ ] Expose typed constants: `NEWSAPI_KEY`, `OPENAI_API_KEY`, `NEWS_MAX_AGE_HOURS`,
      `NEWS_MIN_SOURCES`, `OPENAI_MODEL`

### T-05 Create models.py
- [ ] Define `VerificationStatus` enum: `VERIFIED`, `UNVERIFIED`, `SPECULATIVE`
- [ ] Define `DraftStatus` enum: `DRAFT`, `READY_FOR_REVIEW`, `REJECTED`
- [ ] Define `Story` dataclass with all fields from the design document
- [ ] Define custom exceptions: `StoryRejected`, `DuplicateStory`, `NewsSourceError`,
      `GenerationError`

---

## Task Group 2 — News Fetching

### T-06 Implement news/fetcher.py
- [ ] Implement `fetch_news() -> list[Story]`
- [ ] Call NewsAPI `/v2/top-headlines` with `language=en`, `pageSize=20`
- [ ] Map API response fields to `Story` dataclass fields
- [ ] Skip articles where `title` is `None`, `[Removed]`, or URL is missing
- [ ] Handle HTTP errors and API error responses — raise `NewsSourceError` on failure
- [ ] Log the number of articles fetched

### T-07 Add RSS fallback (optional but recommended)
- [ ] Implement `fetch_news_rss(feed_url: str) -> list[Story]` using `feedparser`
- [ ] Use BBC World or Reuters RSS as the default fallback URL (configurable)
- [ ] Called automatically when NewsAPI returns 0 results or raises `NewsSourceError`

---

## Task Group 3 — Pipeline Steps

### T-08 Implement pipeline/selector.py
- [ ] Implement `select_story(stories: list[Story]) -> Story`
- [ ] Sort by `published_at` descending
- [ ] Return the first story that passes basic checks (title present, URL present,
      not `[Removed]`)
- [ ] Raise `StoryRejected` if no acceptable story is found

### T-09 Implement pipeline/deduplicator.py
- [ ] Implement `load_seen() -> dict` — reads `seen_stories.json`
- [ ] Implement `is_duplicate(story: Story, seen: dict) -> bool`
  - [ ] Exact URL match
  - [ ] Title similarity ≥ 85% using `difflib.SequenceMatcher`
- [ ] Implement `mark_seen(story: Story, seen: dict) -> None` — appends and writes back
- [ ] Raise `DuplicateStory` from the pipeline if duplicate detected

### T-10 Implement pipeline/verifier.py
- [ ] Implement `verify_story(story: Story) -> Story`
- [ ] Query NewsAPI again using top 3 keywords from the title
- [ ] Collect distinct source domains from results
- [ ] Set `story.corroborating_sources` from matching results
- [ ] Set `story.verification_status`:
  - [ ] 2+ distinct sources → `VERIFIED`
  - [ ] 1 source only → `UNVERIFIED`
  - [ ] 0 external sources AND original source is unreliable → `SPECULATIVE`
- [ ] Raise `StoryRejected` with reason if `SPECULATIVE`
- [ ] Define a small `UNRELIABLE_DOMAINS` list in the module

### T-11 Implement pipeline/generator.py
- [ ] Implement `generate_post(story: Story) -> Story`
- [ ] Build a structured prompt with: title, source, published_at, summary,
      verification_status, corroborating sources
- [ ] Include clear instructions: no verbatim copying, label unconfirmed claims,
      factual headline, 4–8 hashtags, suitable for general worldwide audience
- [ ] Call `openai.chat.completions.create()` with the configured model
- [ ] Parse the response to extract post body and hashtag list
- [ ] Set `story.post_content` and `story.hashtags`
- [ ] Raise `GenerationError` on API failure

---

## Task Group 4 — Draft Output

### T-12 Implement output/draft_writer.py
- [ ] Implement `save_draft(story: Story) -> str` — returns the draft file path
- [ ] Determine `draft_status`:
  - [ ] `VERIFIED` → `READY_FOR_REVIEW`
  - [ ] `UNVERIFIED` → `DRAFT`
  - [ ] Rejected stories → `REJECTED` (with reason)
- [ ] Generate a URL-safe slug from the title (lowercase, hyphens, max 60 chars)
- [ ] Write `drafts/YYYY-MM-DD_HH-MM_<slug>.json`
- [ ] Write `drafts/YYYY-MM-DD_HH-MM_<slug>.md` (human-readable preview)
- [ ] Create `drafts/` directory if it does not exist
- [ ] Log the path of the saved draft

### T-13 Define Markdown template
- [ ] The `.md` file should include a clearly formatted preview:
  - Draft status banner
  - Title
  - Source name + URL + publication time
  - Verification status
  - Facebook post content (in a box/blockquote)
  - Hashtags
  - Rejection reason (if applicable)

---

## Task Group 5 — Orchestration

### T-14 Implement agent.py
- [ ] Import and call each pipeline step in order
- [ ] Wrap each step in try/except for its specific exception
- [ ] On `DuplicateStory`: skip and try next candidate from the fetched list
- [ ] On `StoryRejected`: save a REJECTED draft, log the reason, exit cleanly
- [ ] On `NewsSourceError` or `GenerationError`: log the error, exit with code 1
- [ ] On success: log the draft path and status, exit with code 0
- [ ] Accept a `--dry-run` flag that runs the full pipeline but skips writing files
      (useful for testing)

---

## Task Group 6 — Testing

### T-15 Write unit tests for deduplicator.py
- [ ] Test exact URL match returns True
- [ ] Test similar title (≥ 85%) returns True
- [ ] Test unrelated story returns False

### T-16 Write unit tests for verifier.py
- [ ] Test that 2+ sources sets VERIFIED
- [ ] Test that 1 source sets UNVERIFIED
- [ ] Test that SPECULATIVE raises StoryRejected

### T-17 Write unit tests for draft_writer.py
- [ ] Test JSON output contains all required fields
- [ ] Test Markdown file is created
- [ ] Test VERIFIED story gets READY_FOR_REVIEW status
- [ ] Test UNVERIFIED story gets DRAFT status

### T-18 Integration smoke test
- [ ] With real API keys in `.env`, run `python agent.py`
- [ ] Verify a draft file is created in `drafts/`
- [ ] Verify `seen_stories.json` is updated
- [ ] Run again and verify the same story is detected as a duplicate

---

## Task Group 7 — Documentation

### T-19 Write README.md
- [ ] Project overview and Phase 1 description
- [ ] Prerequisites (Python 3.11+, API keys)
- [ ] Setup instructions (clone, pip install, copy .env.example)
- [ ] How to run
- [ ] How to read a draft file
- [ ] How to run tests
- [ ] Roadmap (phases 2–7 in one sentence each)

---

## Dependency Order

```
T-01 → T-02 → T-03 → T-04 → T-05
                               │
              ┌────────────────┤
              │                │
           T-06              T-08
           T-07              T-09
              │              T-10
              └──────────────T-11
                               │
                             T-12
                             T-13
                               │
                             T-14
                               │
                    T-15, T-16, T-17, T-18
                               │
                             T-19
```

All tasks within a group that share the same dependency level can be implemented in
parallel if working in a team.
