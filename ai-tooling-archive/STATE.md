# Project State

> Last Updated: 2026-10-15

## Current Position
- **Milestone:** Core platform built, still in progress (not feature-complete)
- **Status:** Codebase re-mapped; `.gsd/ARCHITECTURE.md` and `.gsd/STACK.md` now match the code
- **Next Command:** `/new-project` (Deep Questioning -> `SPEC.md` -> `ROADMAP.md`) to plan the remaining work in "Known Gaps" of ARCHITECTURE.md

---

## Last Session Summary
Re-mapped the codebase. Previous docs (2026-10-07) were stale.
- **3 services + Postgres/pgvector:** `FS-frontend` (Streamlit), `FS-backend` (FastAPI gateway, auth, ingestion), `FS-AI-Engine` (ingestion NLP + LangGraph chat).
- **Working:** account auth with per-user data isolation; link-based Fireflies ingestion (idempotent, 4-tier persistence with embeddings); chat with catalog answers, participant involvement, content Q&A, date-aware meeting resolution, conversation history, and source-aligned citations; DeepEval benchmark suite.
- **Main gaps:** no docker-compose, no webhook/raw-media end-to-end ingestion, no ingestion job status, retrieval does not yet use topics/summaries/full-text index or a reranker, summarizer LLM path bypasses the Gemini client, tests only for the engine.
