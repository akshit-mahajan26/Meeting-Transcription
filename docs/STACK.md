# Technology Stack

> Re-mapped against the code on 2026-10-15. Versions are minimums from each service's `requirements.txt` / `pyproject.toml`.

## Runtime

| Technology | Purpose |
|------------|---------|
| Python >=3.11 | All three services |
| uv | Package/environment manager |
| Docker | One `Dockerfile` per service (non-root user, `HEALTHCHECK`, `.dockerignore`; no compose file yet) |
| Ruff | Lint/format (`ruff.toml`, per-service `githooks/pre-commit`) |

---

## Dependencies by Service

### FS-backend (:8000)
| Package | Purpose |
|---------|---------|
| `fastapi`, `uvicorn[standard]` | API gateway |
| `pydantic`, `pydantic-settings` | Schemas and config |
| `sqlalchemy` (sync) | ORM / sessions |
| `alembic` | Migrations (head stored in a schema comment, see `core/schema_versioning.py`) |
| `pgvector` | `Vector(1536)` column type |
| `bcrypt`, `pyjwt` | Password hashing, HS256 access tokens |
| `httpx` | Calls to FS-AI-Engine and Fireflies |
| `python-multipart` | Form support |

### FS-AI-Engine (:8001)
| Package | Purpose |
|---------|---------|
| `fastapi`, `uvicorn[standard]` | Engine API |
| `langgraph` | Chat pipeline `StateGraph` |
| `openai` (`AsyncOpenAI`) | Client for Gemini's OpenAI-compatible endpoint (LLM + embeddings) |
| `sqlalchemy` (async), `asyncpg`, `greenlet` | Async DB access for retrieval |
| `pgvector` | Vector similarity (`<=>` cosine distance) |
| `tiktoken` | Declared; token-aware chunking not wired in |
| `httpx` | Summarizer LLM call |
| `pydantic`, `pydantic-settings`, `python-dotenv` | Schemas and config |

### FS-frontend (:8501)
| Package | Purpose |
|---------|---------|
| `streamlit` (>=1.38) | UI (`st.Page` / `st.navigation`) |
| `httpx` | Backend client |
| `pandas` | Tables |
| `pydantic`, `pydantic-settings`, `python-dotenv` | Config |

### Evaluation
| Package | Purpose |
|---------|---------|
| DeepEval | Golden-dataset benchmark (`evaluations/run_deepeval_evaluation.py`) |

Testing: `pytest`, `pytest-asyncio`. Tests currently exist for FS-AI-Engine only.

---

## Infrastructure & External Services

| Service | Provider | Purpose |
|---------|----------|---------|
| Transcripts | Fireflies.ai (GraphQL) | Fetch transcripts; upload audio for transcription |
| LLM | Google Gemini (`gemini-2.5-flash` default) via OpenAI-compatible endpoint | Query rewrite, answer synthesis, topic titles |
| Embeddings | Google Gemini `gemini-embedding-001`, 1536 dims | Turn and query vectors |
| Database | PostgreSQL + `pgvector` extension | Relational tiers, HNSW cosine index, GIN full-text column |

---

## Configuration & Environment Variables

| Variable | Service | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | backend, engine | PostgreSQL connection (backend normalizes to `postgresql+psycopg://`, engine to `postgresql+asyncpg://`) |
| `ENGINE_API_KEY` | backend, engine | Shared secret (>=16 chars) in `X-Internal-Api-Key` |
| `AI_ENGINE_URL`, `AI_ENGINE_TIMEOUT_SECONDS` | backend | Engine location and timeout (default 300s for ingestion) |
| `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | backend | Login tokens |
| `FIREFLIES_API_KEY`, `FIREFLIES_API_URL` | backend | Fireflies access |
| `CORS_ORIGINS` | backend | Allowed origins (default `*`) |
| `ALLOW_SIGNUP` | backend | `false` blocks new account registration (default `true`) |
| `BOUNDARY_SIMILARITY_THRESHOLD` | engine | Topic boundary cutoff for segmentation (default 0.55) |
| `MAX_TURN_TOKENS` | engine | Split a speaker's turn after this many words (default 0 = never) |
| `CHUNK_OVERLAP_SENTENCES` | engine | Sentences repeated at the start of the next turn after such a split (default 0) |
| `RETRIEVAL_CANDIDATE_LIMIT` | engine | Max similar turns fetched per vector search (default 1000, `0` = no cap) |
| `GEMINI_API_KEY` (alias `GOOGLE_API_KEY`), `GEMINI_BASE_URL` | engine | LLM + embeddings |
| `LLM_MODEL_NAME`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | engine | Model selection (dims must match the `vector(1536)` column) |
| `FASTAPI_BACKEND_URL` | frontend | Backend base URL |

Segmentation tuning (defaults in `engine/core/config.py`): `topic_similarity_threshold=0.55` (env `BOUNDARY_SIMILARITY_THRESHOLD`), `min_turns_per_segment=2`, `max_turns_per_segment=10`.
