# Requirements — Facebook News Agent

## Project Overview

An automated system that discovers recent worldwide news, evaluates and verifies it, generates
original Facebook post content, and saves it as a draft for human review.

---

## Phase 1 Scope

Phase 1 produces a single draft file per run. Nothing is published automatically.

---

## Functional Requirements

### FR-01 — News Discovery
- The system must retrieve recent worldwide news from at least one reliable external source.
- Supported sources (Phase 1): NewsAPI or RSS feeds from reputable outlets (BBC, Reuters, AP).
- News must be no older than 24 hours unless no recent news is available.

### FR-02 — Story Selection
- The system must select one story per run.
- Selection criteria: recency, relevance to a general worldwide audience, availability of
  verifiable facts.
- The system must prefer stories covered by multiple independent sources.

### FR-03 — Information Collection
- For the selected story, the system must collect:
  - Title
  - Source name and URL
  - Publication time
  - Summary or article body (if accessible)
  - Additional corroborating sources when available

### FR-04 — Verification
- The system must assess whether a story can be sufficiently verified.
- Verification levels:
  - VERIFIED: covered by 2+ reliable sources with consistent facts
  - UNVERIFIED: only one source, or facts are inconsistent across sources
  - SPECULATIVE: story is based on rumour, prediction, or anonymous claims
- Stories marked SPECULATIVE must be rejected.
- Stories marked UNVERIFIED must be flagged but may still produce a DRAFT (not READY_FOR_REVIEW).

### FR-05 — Duplicate Detection
- Before generating content, the system must check whether the story has already been processed.
- A simple local log file (seen_stories.json) is sufficient for Phase 1.
- Duplicates are identified by URL or by title similarity (fuzzy match).
- If a duplicate is detected, the system must skip the story and select another.

### FR-06 — Content Generation
- The system must generate an original Facebook post. It must NOT copy article text verbatim.
- The post must contain:
  1. A strong, factual headline (not clickbait)
  2. A concise summary (2–4 sentences)
  3. The key facts
  4. Context where necessary
  5. A short closing statement
  6. Relevant hashtags (4–8 tags)
- The writing must be clear and suitable for a general worldwide audience.
- Unconfirmed information must be clearly labelled (e.g. "reportedly", "according to sources").

### FR-07 — Draft File Output
- The system must save the result as a structured draft file.
- Format: JSON (machine-readable) + Markdown (human-readable preview).
- Location: `drafts/` directory.
- Filename: `YYYY-MM-DD_HH-MM_<slug>.json` (and matching `.md`).

### FR-08 — Draft Status
- Every draft must carry one of three statuses:
  - `DRAFT` — generated but not reviewed
  - `READY_FOR_REVIEW` — verified, passes quality checks, ready for a human to approve
  - `REJECTED` — failed quality or safety checks; reason must be recorded

### FR-09 — Safety Gates
The system must reject a story (status = REJECTED) when:
- The story cannot be sufficiently verified (SPECULATIVE).
- Sources significantly contradict each other on core facts.
- The story is identified as probable misinformation.
- The source is on a known-unreliable list.
- The content is too speculative or based entirely on anonymous claims.

For breaking news, at least 2 reliable sources must confirm the story before it becomes
READY_FOR_REVIEW.

---

## Non-Functional Requirements

### NFR-01 — Simplicity
Phase 1 must be the smallest working implementation. No over-engineering.

### NFR-02 — Extensibility
The architecture must make it straightforward to add Phase 2–7 features without rewriting
the core.

### NFR-03 — Transparency
Every draft must record why it was accepted or rejected. Logs must be human-readable.

### NFR-04 — No Automatic Publishing
Phase 1 must never post anything to Facebook, call the Facebook Graph API, or take any
action that affects a live system.

### NFR-05 — Reliability
The system must handle API failures, rate limits, and network errors gracefully without
crashing. It must log errors and exit cleanly.

### NFR-06 — Configuration
All secrets (API keys), tunable parameters (news age limit, minimum sources), and file
paths must be stored in a `.env` file and/or a `config.py`. They must not be hardcoded.

---

## Out of Scope for Phase 1

- Facebook publishing
- Image generation
- Video / Reel generation
- Scheduling
- Analytics
- Multiple categories or topic filters
- News scoring / ranking
- Web UI or dashboard
