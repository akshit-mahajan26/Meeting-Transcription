# Meeting Transcript Intelligence Platform

Turn recorded meetings into a knowledge base you can question in plain English.
Paste a Fireflies.ai transcript link, and the platform fetches, structures, embeds and stores it.
Then ask questions in a chat and get short answers with citations to the exact speaker and moment.

> **Status: work in progress.** Sign-in, ingestion, and cited question answering work end to end.
> See [Known gaps](#known-gaps) for what is not done yet.

## Features

- **Link-based ingestion** of Fireflies transcripts, with idempotent re-ingestion (content hash) and a single-transaction write, so a failed ingestion never leaves a half-loaded meeting.
- **4-tier structure** per meeting: project → meeting (summary) → topic segments → speaker turns with 1536-d embeddings.
- **Grounded chat** (LangGraph RAG): question rewrite, LLM-based intent routing, pgvector retrieval, answer synthesis, and citations to only the excerpts the model used.
- **Question types:** inventory ("how many meetings…"), participant ("which meetings did X attend…"), content (decisions, commitments, summaries), date-aware and follow-up questions, plus small talk without a search.
- **Per-account isolation:** every row carries a `user_id`, and every engine query filters on it.
- **Graceful degradation:** LLM or embedding failures fall back to heuristics instead of failing the request.

## Architecture

Three Python services plus PostgreSQL:

```
FS-frontend   (Streamlit, :8501)   Login · Overview · Assistant · Meetings · Ingest
     │  REST + Bearer JWT
FS-backend    (FastAPI,   :8000)   Auth · ownership scoping · Fireflies client · ingestion orchestrator · query proxy · Alembic migrations
     │  REST + X-Internal-Api-Key
FS-AI-Engine  (FastAPI,   :8001)   Ingestion NLP (normalize, tag, embed, segment, summarize) · LangGraph RAG
     │
PostgreSQL + pgvector              users · projects → meetings → meeting_topics → speaker_turns (HNSW vector index)

External: Fireflies.ai (GraphQL) · Google Gemini (LLM + embeddings via its OpenAI-compatible endpoint)
```

Design rules:

- The backend never does AI work, and the engine never does auth. The engine trusts only calls carrying the shared `X-Internal-Api-Key` and a `user_id` that the backend set from the verified JWT.
- The frontend has no database or LLM access; everything goes through [FS-frontend/services/api_client.py](FS-frontend/services/api_client.py).

For the full component breakdown, data model and data flows, see [.gsd/ARCHITECTURE.md](.gsd/ARCHITECTURE.md).
For a non-technical overview, see [docs/architecture_notion.md](docs/architecture_notion.md).

## Repository layout

| Path | What it is |
|------|------------|
| [FS-frontend/](FS-frontend/) | Streamlit UI (`app.py`, `app_pages/`, `ui/`, `services/api_client.py`) |
| [FS-backend/](FS-backend/) | FastAPI gateway: auth, meetings, ingestion, query proxy, Alembic migrations |
| [FS-AI-Engine/](FS-AI-Engine/) | FastAPI engine: ingestion NLP (`services/`) and the chat graph (`graphs/`) |
| [docs/](docs/) | Architecture overview and project deep dive |
| [.gsd/](.gsd/) | Architecture, stack, and state notes |
| [ruff.toml](ruff.toml) | Shared lint/format config |

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector) extension
- A Google Gemini API key
- A Fireflies.ai API key

## Getting started

### 1. Start PostgreSQL with pgvector

The quickest way is the official pgvector image:

```bash
docker run -d --name meeting-db \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=meeting_intelligence \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

You don't need to create tables. The backend enables the `vector` extension and runs Alembic migrations on startup.

### 2. Configure environment variables

Each service has its own `.env.example`. Copy each one and fill in the values:

```bash
cp FS-backend/.env.example   FS-backend/.env
cp FS-AI-Engine/.env.example FS-AI-Engine/.env
cp FS-frontend/.env.example  FS-frontend/.env
```

The values that matter most:

| Variable | Service | Notes |
|----------|---------|-------|
| `DATABASE_URL` | backend, engine | Same database for both. Backend uses `postgresql://…`; engine uses `postgresql+asyncpg://…` |
| `ENGINE_API_KEY` | backend, engine | Shared secret of at least 16 characters; **must match** in both files |
| `JWT_SECRET` | backend | Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `FIREFLIES_API_KEY` | backend | Used to fetch transcripts |
| `GEMINI_API_KEY` | engine | LLM and embeddings. Without it, embeddings fall back to deterministic placeholder vectors |
| `AI_ENGINE_URL` | backend | Default `http://localhost:8001` |
| `FASTAPI_BACKEND_URL` | frontend | Default `http://localhost:8000` |
| `ALLOW_SIGNUP` | backend | Set to `false` to disable new account registration |

### 3. Install dependencies (first time only)

```bash
(cd FS-AI-Engine && uv sync)
(cd FS-backend   && uv sync)
(cd FS-frontend  && uv sync)
```

### 4. Run the services

Start each one in its own terminal, in this order:

```bash
cd FS-AI-Engine && uv run python -m engine.main    # AI engine  → :8001
cd FS-backend   && uv run python -m app.main       # backend    → :8000 (runs DB migrations on startup)
cd FS-frontend  && uv run streamlit run app.py     # frontend   → :8501
```

The engine and backend read their host, port and auto-reload setting from their `.env`, so you don't pass any flags:

| Variable | Default | Effect |
|----------|---------|--------|
| `AI_ENGINE_HOST` / `BACKEND_HOST` | `0.0.0.0` | Listens on all interfaces, so other devices on your network can reach it |
| `AI_ENGINE_PORT` / `BACKEND_PORT` | `8001` / `8000` | Port |
| `AI_ENGINE_ENV` / `BACKEND_ENV` | `development` | `development` turns on auto-reload when code changes |

Open http://localhost:8501, create an account, paste a Fireflies transcript link on the **Ingest** page, and ask questions on the **Assistant** page.

Interactive API docs are at http://localhost:8000/docs (backend) and http://localhost:8001/docs (engine).

### Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| Backend exits with `Database migration failed; refusing to start` and `Connection refused` on the database port | PostgreSQL isn't running, or `DATABASE_URL` points at the wrong port. Check with `pg_isready -h localhost -p <port>`. |
| Homebrew PostgreSQL won't start; its log repeats `lock file "postmaster.pid" already exists` | A stale lock file is left over from a crash or reboot. Make sure no `postgres` process is running, then delete `postmaster.pid` from the data directory (e.g. `/opt/homebrew/var/postgresql@17/`) and run `brew services restart postgresql@17`. |
| Login page shows `Cannot reach the backend: [Errno 61] Connection refused` | The backend isn't running, or `FASTAPI_BACKEND_URL` in `FS-frontend/.env` doesn't match where it listens. Use `http://localhost:8000` when everything runs on one machine, then restart Streamlit (it reads `.env` only at startup). |
| `uv` warns that `VIRTUAL_ENV ... does not match the project environment` | Another service's virtualenv is still active in that shell. It's harmless; run `deactivate` to silence it. |

### Running with Docker

Each service has its own `Dockerfile` (non-root user, health check). There is no `docker-compose.yml` yet, so build and run them individually:

```bash
docker build -t fs-ai-engine FS-AI-Engine
docker build -t fs-backend   FS-backend
docker build -t fs-frontend  FS-frontend
```

When running in containers, point `DATABASE_URL`, `AI_ENGINE_URL` and `FASTAPI_BACKEND_URL` at the container hostnames instead of `localhost`.

## API overview

**Backend** (`/api/v1`; everything except `/auth/*` needs `Authorization: Bearer <token>`):

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/auth/signup` | Create an account (403 when signup is disabled) |
| `POST` | `/auth/login` | Get an access token |
| `GET` | `/auth/me` | Current user |
| `GET` | `/meetings/` | List meetings |
| `GET` | `/meetings/projects` | List projects |
| `GET` | `/meetings/{id}` | Meeting detail |
| `GET` | `/meetings/{id}/turns` | Speaker turns for a meeting |
| `POST` | `/meetings/ingest-link` | Queue a transcript for ingestion (202) |
| `POST` | `/query` | Ask a question (proxied to the engine) |
| `GET` | `/health` | Health check |

**Engine** (internal only; needs `X-Internal-Api-Key`):

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/transcript/process-full` | Full ingestion pipeline: normalize → tag signals → embed + segment → summarize |
| `POST` | `/api/v1/transcript/embed` | Batch embeddings |
| `POST` | `/api/v1/engine/query` | LangGraph RAG query |
| `GET` | `/health` | Health check |

## Development

- **Lint and format:** `ruff check .` and `ruff format .` from the repo root (config in [ruff.toml](ruff.toml)).
- **Pre-commit hooks:** each service ships a `githooks/pre-commit` that, among other checks, caps Python files at 400 lines. Install the one for the service you work on, for example:
  ```bash
  cp FS-backend/githooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
  ```
- **Tests:** engine tests run with `cd FS-AI-Engine && uv run pytest`. Test directories are gitignored, so they exist only in local checkouts. The backend and frontend have no tests yet.

## Known gaps

Main items still open (the full list is in [.gsd/ARCHITECTURE.md](.gsd/ARCHITECTURE.md#known-gaps--technical-debt-project-not-complete)):

- No root `docker-compose.yml`.
- Raw audio/video links are sent to Fireflies for transcription but not ingested automatically afterwards; there is no Fireflies webhook receiver.
- Retrieval ranking is a hand-tuned heuristic (vector + keyword + speaker boost). Topic segments and the full-text index are stored but not yet used in retrieval.
- Decisions, commitments and blockers are extracted during ingestion but not persisted.
- The evaluation harness measures answer availability, not correctness.
- CORS defaults to `*`.
