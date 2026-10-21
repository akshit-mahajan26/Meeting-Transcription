# Architecture

> Re-mapped against the code on 2026-10-15 (previous version, 2026-10-07, was stale: it listed modules that no longer exist).
> Status: **work in progress.** Ingestion, auth, and the chat pipeline work end to end; items under "Known Gaps" are not done.

## Overview

The **Meeting Transcript Intelligence Platform** ingests multi-speaker meeting transcripts (from Fireflies.ai), structures them into a 4-tier hierarchy with vector embeddings, and answers natural-language questions about them with cited, grounded answers. Every account only ever sees its own data.

Three services plus PostgreSQL:

```
┌──────────────────────────────────────────────────────────────┐
│  FS-frontend (Streamlit :8501)                               │
│  Login · Overview · Assistant · Meetings · Ingest            │
└───────────────────────────────┬──────────────────────────────┘
                                │ HTTPS/REST, Bearer JWT
┌───────────────────────────────▼──────────────────────────────┐
│  FS-backend (FastAPI :8000)  - gateway + ingestion           │
│  Auth (bcrypt/JWT) · ownership scoping · Fireflies client    │
│  ingestion orchestrator · query proxy · Alembic migrations   │
└───────────────┬───────────────────────────────┬──────────────┘
                │ REST + X-Internal-Api-Key     │ SQLAlchemy (sync)
┌───────────────▼──────────────┐                │
│  FS-AI-Engine (FastAPI :8001)│                │
│  /transcript/*  ingestion NLP│                │
│  /engine/query  LangGraph RAG│                │
└───────────────┬──────────────┘                │
                │ SQLAlchemy (async, asyncpg)   │
┌───────────────▼───────────────────────────────▼──────────────┐
│  PostgreSQL + pgvector                                       │
│  users · projects → meetings → meeting_topics → speaker_turns│
│  speaker_turns: vector(1536) HNSW index, tsvector GIN index  │
└──────────────────────────────────────────────────────────────┘
External: Fireflies.ai (GraphQL) · Google Gemini (LLM + embeddings, OpenAI-compatible endpoint)
```

Key design rules:
- **Backend never does AI work; engine never does auth.** The backend proxies queries; the engine trusts only calls carrying the shared `X-Internal-Api-Key` and a `user_id` the backend set from the verified JWT.
- **Frontend has no DB or LLM access**; everything goes through `services/api_client.py`.
- **Per-account isolation:** every data row has `user_id`; every engine SQL query filters on it. Project IDs are namespaced `{user_id}:{name}`.

---

## Components

### 1. FS-frontend (Streamlit)
- Entry: [app.py](../FS-frontend/app.py). Nothing renders until login succeeds (`ui/auth.py`).
- Pages (`app_pages/`): `overview`, `assistant` (chat + citations), `meetings` (browse meetings and turns), `ingest` (submit a Fireflies/media link).
- `services/api_client.py`: `MeetingIntelligenceClient` (login, signup, query_backend, list_projects, list_meetings, get_meeting, ingest_link). A 401 expires the session and returns to login.
- `ui/`: styles (`style.css`), shell, nav, session state, scope panel (project/meeting filter), components.

### 2. FS-backend (gateway + ingestion)
- Entry: [app/main.py](../FS-backend/app/main.py). Lifespan runs `init_db()`: enables `vector` extension, runs Alembic to head under a Postgres advisory lock so concurrent replicas migrate one at a time (stamps legacy pre-Alembic DBs at baseline). A missing `alembic.ini` aborts startup; there is no `create_all` fallback.
- Routes under `/api/v1`, all protected by `get_current_user` except `/auth/*`:
  - `auth`: `POST /auth/signup` (returns 403 when `ALLOW_SIGNUP=false`), `/auth/login`, `GET /auth/me`.
  - `meetings`: `GET /meetings/`, `/meetings/projects`, `/meetings/{id}`, `/meetings/{id}/turns`.
  - `POST /meetings/ingest-link` (202, background task) in `api/routers/meetings.py`.
  - `POST /query` (pass-through proxy to engine) in `api/routers/query.py`.
- Services: `fireflies_client.py` (GraphQL fetch transcript / upload audio), `pipeline_orchestrator.py` (idempotency hash, engine dispatch with 3 retries + exponential backoff, atomic 4-tier upsert), `ownership.py` (project namespacing, cross-account ID collision checks), `meeting_service.py` (read APIs).
- `core/security.py`: bcrypt password hashing, HS256 JWT. `core/schema_versioning.py`: stores Alembic head in a Postgres schema comment instead of an `alembic_version` table.

### 3. FS-AI-Engine (intelligence core)
- Entry: [engine/main.py](../FS-AI-Engine/engine/main.py); all routes require `verify_internal_key`.
- **Ingestion NLP** (`/api/v1/transcript/*`, `api/routes.py`; main entry `POST /process-full`):
  - `normalizer.py`: sort sentences, merge consecutive same-speaker sentences into Level 1 turns, deterministic IDs `mtg_<id>_turn_<NNN>`.
  - `signal_tagger.py`: regex flags for question / commitment / blocker per turn.
  - `embedding_service.py`: Gemini `gemini-embedding-001` at 1536 dims, batches of 50, 4 concurrent; deterministic hash-based fallback vectors when no API key.
  - `segmenter.py`: Level 2 topic segments. Bag-of-words cosine similarity over sliding windows, threshold 0.55, min 2 / max 10 turns, short-tangent guard, LLM 5-6 word titles (heuristic fallback), 6 concurrent.
  - `summarizer.py`: Level 3 summary (overview, decisions, commitments with owner, blockers), each grounded to turn IDs. LLM extraction with hallucinated-ID guardrail, deterministic regex fallback.
  - Helper endpoints: `/normalize`, `/embed`, `/tag-signals`, `/segment-topics`, `/extract-intelligence`.
- **Chat / RAG** (`POST /api/v1/engine/query` → `services/rag_service.py` → `graphs/chat_graph.py`), LangGraph `StateGraph` over `ChatState`:
  - `extract_filters` (`chat_nodes.py`): loads the account's meetings/projects/speakers, then two LLM calls: `rewrite_query` (`chat_rewrite.py`: typo fix, follow-up resolution only) and `analyze_query` (`chat_analysis.py`: one routing prompt returning intent, speaker, meeting, project, time scope, date range, catalog entities, `needs_retrieval`, small-talk reply). Returned names/IDs are validated against the account's records; on failure the default is `content_qa` with full retrieval.
  - Intents (`chat_query.py`): `catalog`, `participant_involvement`, `content_qa`, `smalltalk`.
  - `embed_query` → `vector_retrieval` (`chat_retrieval.py`): pgvector cosine similarity + keyword overlap + speaker boost − filler penalty; stratified, speaker-diverse selection across meetings.
  - `llm_synthesis` (`chat_synthesis.py`): grounded prompt with numbered excerpts, catalog/meeting-detail records (`chat_records.py`), current date, history; model reports `SOURCES:` line.
  - `format_citations` (`chat_citations.py`): cites exactly the excerpts the model used (heuristic alignment fallback). Turn-level citations if a meeting is scoped, one per meeting otherwise.
  - `fallback`: no data / no meeting on requested date.
  - `llm_client.py`: Gemini via OpenAI-compatible endpoint.

### 4. Evaluations
- `evaluations/`: DeepEval golden dataset (50 cases, 10 scenario archetypes incl. hallucination resistance), legacy 20-case dataset, runners `run_deepeval_evaluation.py` and `run_evaluations.py`, plan in `MEETING_TRANSCRIPTION_EVALUATION_PLAN.md`.

### 5. Repo tooling
- `adapters/`, `scripts/validate-*`, `GSD-STYLE.md`, `PROJECT_RULES.md`, `model_capabilities.yaml`: GSD agent-workflow tooling, not part of the runtime.

---

## Data Model (PostgreSQL)

| Table | Tier | Key columns |
|-------|------|-------------|
| `users` | - | id (uuid), email (unique), password_hash |
| `projects` | 1 | id (`{user_id}:{name}`), user_id, name, status |
| `meetings` | 2 (Level 3 summary) | id (Fireflies ID), user_id, project_id, title, summary, content_hash, date, duration, status |
| `meeting_topics` | 3 (Level 2 segment) | id (uuid), meeting_id, topic_index, title, compiled_text, start/end_time |
| `speaker_turns` | 4 (Level 1 turn) | turn_id, meeting_id, topic_id, speaker_name, text, start/end_time, is_question/commitment/blocker, `embedding vector(1536)`, `metadata jsonb`, `tsv tsvector` |

Indexes: HNSW (`vector_cosine_ops`) on embedding; GIN on `tsv`. Migrations: `e0d6e74c6561` (initial), `a3f1c9d27b40` (users + ownership).

---

## Data Flow

### Ingestion
```
Frontend /ingest → POST /api/v1/meetings/ingest-link (JWT)
  → parse link (Fireflies dashboard | raw media) ; 409 if meeting ID owned by another account
  → 202 queued ; BackgroundTask:
      FirefliesClient.fetch_transcript_by_id (GraphQL)
      Orchestrator: SHA-256 content hash → skip if same hash & PROCESSED (idempotent)
      POST engine /transcript/process-full  (retry x3, backoff 1s,2s,4s)
          normalize → tag signals → [embed turns ‖ segment topics] → summarize
      Backfill missing embeddings via /embed
      Single DB transaction: claim project → upsert meeting (delete old topics/turns on re-ingest)
          → insert topics → map turns to topics by time window → bulk insert turns
```
Raw-media links are only forwarded to Fireflies `uploadAudio`; the resulting transcript is not ingested automatically (see Known Gaps).

### Query
```
Frontend /assistant → POST /api/v1/query (JWT, history)
  → backend resolves project scope, sets user_id from JWT, forwards to engine with internal key
  → LangGraph:
      extract_filters ─┬─ greeting / pleasantry ─────────→ smalltalk ─→ END
                       ├─ no meeting on requested date ─→ fallback ─→ END
                       ├─ pure inventory (catalog) ─────→ llm_synthesis (DB records only)
                       └─ otherwise ─→ embed_query → vector_retrieval ─┬─ hits ─→ llm_synthesis
                                                                       └─ none ─→ fallback
      llm_synthesis → format_citations → END
  → answer + citations + routing_mode (LANGGRAPH_RAG | LANGGRAPH_CATALOG | LANGGRAPH_SMALLTALK | LANGGRAPH_FALLBACK)
```

---

## Integration Points

| Service | Purpose | Config |
|---------|---------|--------|
| Fireflies.ai GraphQL | Fetch transcripts, upload audio | `FIREFLIES_API_KEY`, `FIREFLIES_API_URL` |
| Google Gemini (OpenAI-compatible endpoint) | Chat LLM (`gemini-2.5-flash` default) and embeddings (`gemini-embedding-001`, 1536d) | `GEMINI_API_KEY`, `GEMINI_BASE_URL`, `LLM_MODEL_NAME`, `EMBEDDING_MODEL` |
| PostgreSQL + pgvector | Relational store + vector search | `DATABASE_URL` |
| Backend → Engine | Internal REST | `AI_ENGINE_URL`, `ENGINE_API_KEY` (≥16 chars, both services) |
| Frontend → Backend | REST | `FASTAPI_BACKEND_URL` |
| Auth | bcrypt + JWT HS256 | `JWT_SECRET`, `ACCESS_TOKEN_EXPIRE_MINUTES` |

---

## Known Gaps / Technical Debt (project not complete)

- [ ] No root `docker-compose.yml` (each service has its own Dockerfile only).
- [ ] Raw audio/video links are not ingested end to end; no Fireflies webhook receiver exists (ingestion is link-triggered only).
- [ ] Background ingestion failures are only logged; no job status, retry queue, or user-visible error.
- [ ] No hybrid reranker or macro/micro two-tier retrieval: ranking is a heuristic score (3×vector + 1.5×keyword hits + speaker boost). The `tsv` full-text column and Level 2/3 data are stored but **not yet used in retrieval**.
- [ ] Vector retrieval fetches at most `RETRIEVAL_CANDIDATE_LIMIT` (default 1000) turns by similarity, then scores them in Python; `0` removes the cap. Keyword-only search is uncapped, and keyword matches outside the top candidates are dropped.
- [ ] Level 3 decisions/commitments/blockers are computed during ingestion but not persisted (only the overview text is); topic summary text is also dropped because the backend reads `summary`/`compiled_text` while the engine returns `topic_summary`.
- [ ] Evaluation: the DeepEval run records answers but no correctness metric is computed; `evaluations/run_evaluations.py` predates auth and gets HTTP 401.
- [ ] Topic segmentation uses lexical (bag-of-words) similarity, not embeddings; `boundary_similarity_threshold`, `max_turn_tokens`, `chunk_overlap_sentences` settings are unused (no token-aware chunking).
- [ ] Summarizer's LLM path calls `api.openai.com` directly with `llm_api_key` (unlike the rest, which use Gemini) and is skipped when that key is unset, so the heuristic path is what normally runs.
- [ ] Config naming is inconsistent (`OPENROUTER_*` property accessors still referenced in `config.py` though only Gemini is configured; `OpenRouterClient` class actually targets Gemini).
- [ ] Pydantic schemas duplicated between backend and engine.
- [ ] Engine CORS is `*`; backend CORS defaults to `*`.
- [ ] Intent, speaker and meeting resolution are LLM-driven (`chat_analysis.py`) with no rule-based safety net; a wrong routing decision costs accuracy, and every question costs two small LLM calls of latency before synthesis.
- [ ] Tests exist only for the engine (`FS-AI-Engine/tests`: account isolation, catalog routing, history, temporal resolution, core, phase2); none for backend/frontend.

---

## Conventions

- Snake_case modules/vars, PascalCase models. Ruff config at repo root (`ruff.toml`) plus per-service `githooks/pre-commit`.
- Deterministic IDs: `mtg_<id>_turn_<NNN>`, `mtg_<id>_seg_<NN>`, `mtg_<id>_sum`.
- Graceful degradation: LLM/embedding failures fall back to heuristics rather than failing the request.
