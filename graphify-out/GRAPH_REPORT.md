# Graph Report - Meeting-Transcription  (2026-10-30)

## Corpus Check
- Corpus is ~40,968 words - fits in a single context window. You may not need a graph.

## Summary
- 769 nodes · 1599 edges · 58 communities (42 shown, 11 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 189 edges (avg confidence: 0.91)
- Token cost: 12,500 input · 2,100 output

## Community Hubs (Navigation)
- Frontend API Client & Streaming
- Backend Meetings & Projects API
- Hierarchical Pipeline Schemas
- LangGraph Chat State Workflow
- Database Migrations & Alembic Environment
- SQLAlchemy Core Domain Models
- Chat State & Participant Resolution
- AI Engine Ingestion Endpoints
- Backend Query Proxy Endpoint
- AI Engine Query Router & Contracts
- FastAPI Auth Dependencies & Current User
- User Authentication & Registration
- Chat Graph Context Loader & Records
- AI Engine Dynamic Configuration
- Frontend Ingestion UI & Caching
- Structured Logging & Request Tracing
- RAG Synthesis & Grounded Citations
- Topic Segmentation & Heading Generation
- Speaker Turn Normalization & Deep Dive
- Fireflies Ingestion Background Task
- Frontend Meeting Explorer & Detail View
- Backend Configuration & CORS Settings
- Streamlit Chat Assistant & History
- Frontend Authentication & Session State
- Link Parsing & Ingestion Routing
- Query Routing & LLM Analysis
- Frontend UI Components & Styling
- Conversational Query Rewriting
- Fireflies GraphQL API Client
- Hybrid Vector & Keyword Search
- Gemini Embedding Generation Service
- Citation Formatting & Excerpt Alignment
- Frontend App Entry & Overview Dashboard
- Schema-Comment Version Tracker
- LLM Completion Client & Streaming
- Frontend Configuration & Secrets
- Platform Architecture & Ingestion Principles
- Frontend Global CSS & Theme Styling
- Infrastructure Spec & DeepEval Benchmark
- Baseline Database Schema Migration
- User Ownership Migration
- Multi-Tenant Owner Column Helper
- AI Engine Health Check
- AI Engine Package Initialization
- Frontend Git Pre-Commit Hook
- Backend Package Initialization
- Backend Git Pre-Commit Hook
- AI Engine Git Pre-Commit Hook
- Frontend Package Initialization
- Project State & Roadmap Spec
- AI Engine Service Descriptor
- Backend Service Descriptor
- Frontend Service Descriptor

## God Nodes (most connected - your core abstractions)
1. `SpeakerTurn` - 26 edges
2. `ChatState` - 23 edges
3. `Settings` - 22 edges
4. `MeetingIntelligenceClient` - 22 edges
5. `Settings` - 21 edges
6. `get_settings()` - 21 edges
7. `MeetingService` - 21 edges
8. `TopicSegmenter` - 20 edges
9. `SpeakerNormalizer` - 19 edges
10. `UserModel` - 18 edges

## Surprising Connections (you probably didn't know these)
- `Query Rewrite and Intent Analysis` --implements--> `analyze_query()`  [INFERRED]
  docs/architecture_notion.md → FS-AI-Engine/engine/graphs/chat_analysis.py
- `LangGraph Chat Pipeline` --implements--> `build_chat_graph()`  [INFERRED]
  .gsd/ARCHITECTURE.md → FS-AI-Engine/engine/graphs/chat_graph.py
- `Lexical Topic Segmentation` --implements--> `TopicSegmenter`  [INFERRED]
  docs/Project_Deep_Dive.pdf → FS-AI-Engine/engine/services/segmenter.py
- `Ingestion Idempotency Hashing` --implements--> `BackendPipelineOrchestrator`  [INFERRED]
  .gsd/ARCHITECTURE.md → FS-backend/app/services/pipeline_orchestrator.py
- `Multi-Speaker Turn Normalization` --implements--> `SpeakerNormalizer`  [INFERRED]
  docs/Project_Deep_Dive.pdf → FS-AI-Engine/engine/services/normalizer.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Three-Service Platform Architecture** — fs_frontend_services_api_client, fs_backend_app_services_pipeline_orchestrator, fs_ai_engine_engine_graphs_chat_graph [INFERRED 0.85]
- **Ingestion NLP Pipeline (Normalize -> Tag -> Segment -> Summarize)** — fs_ai_engine_engine_services_normalizer_speakernormalizer, fs_ai_engine_engine_services_segmenter_topicsegmenter, fs_ai_engine_engine_services_summarizer_meetingsummarizer [EXTRACTED 1.00]
- **RAG Query Resolution and Synthesis Flow** — fs_ai_engine_engine_graphs_chat_analysis_analyze_query, fs_ai_engine_engine_services_rag_service, fs_ai_engine_engine_graphs_chat_graph_build_chat_graph [EXTRACTED 1.00]

## Communities (58 total, 11 thin omitted)

### Community 0 - "Frontend API Client & Streaming"
Cohesion: 0.09
Nodes (20): _error_detail(), _iter_sse(), MeetingIntelligenceClient, Any, Response, POST credentials to /auth/<path>; returns {"success", "token", "user"} or…, The answer dict the UI works with, from the backend's query response., Sends natural language prompt and optional filters to POST /api/v1/query.… (+12 more)

### Community 1 - "Backend Meetings & Projects API"
Cohesion: 0.10
Nodes (26): get_meeting(), get_meeting_turns(), list_meetings(), list_projects(), get, Thin route handler returning summaries of all meetings., Returns all known project identifiers across meetings and projects., Thin route handler returning meeting by ID. (+18 more)

### Community 2 - "Hierarchical Pipeline Schemas"
Cohesion: 0.13
Nodes (21): GroundedItem, HierarchicalPipelineResult, Level3Summary, NormalizationResult, BaseModel, field_validator, Structured intelligence item (decision, commitment, or blocker) strictly…, Level 3 Executive Meeting Intelligence Node. Strictly follows deterministic ID… (+13 more)

### Community 3 - "LangGraph Chat State Workflow"
Cohesion: 0.12
Nodes (28): build_chat_graph(), Constructs and compiles the production LangGraph chat workflow., analyze_query_node(), extract_filters_node(), lookup_records_node(), Any, Step 2: Rewrite the message into a standalone, typo-corrected question (needs…, Step 3: Analyse the standalone question (intent, entities, dates, retrieval… (+20 more)

### Community 4 - "Database Migrations & Alembic Environment"
Cohesion: 0.12
Nodes (21): get_url(), Run migrations in 'offline' mode., Run migrations in 'online' mode., run_migrations_offline(), run_migrations_online(), get_settings(), Return cached singleton settings., init_db() (+13 more)

### Community 5 - "SQLAlchemy Core Domain Models"
Cohesion: 0.12
Nodes (26): Base, MeetingModel, ProjectModel, Tier 1: Enterprise Project Entity., Tier 2: Ingested Meeting Entity (Level 3 Summary)., friendly_failure_reason(), Exception, Background ingestion failures: the error type, a user-safe reason, and… (+18 more)

### Community 6 - "Chat State & Participant Resolution"
Cohesion: 0.11
Nodes (21): datetime, Resolved participant names carried in the graph state (plural list, or the…, _state_speakers(), AnswerStreamFilter, _could_be_sources_line(), Parsing and streaming-time cleanup of the `SOURCES:` line and inline `[n]`…, True while `line` is still compatible with a `SOURCES: ...` line (so it must…, Cleans a streamed answer the way `split_sources_line` cleans the final one, so… (+13 more)

### Community 7 - "AI Engine Ingestion Endpoints"
Cohesion: 0.12
Nodes (20): embed_texts_endpoint(), EmbedTextsRequest, EmbedTextsResponse, get_meeting_summarizer(), get_signal_tagger(), _has_native_summary(), normalize_transcript(), process_full_pipeline() (+12 more)

### Community 8 - "Backend Query Proxy Endpoint"
Cohesion: 0.15
Nodes (23): Citation, _engine_headers(), _engine_http_error(), _engine_payload(), _engine_url(), HistoryMessage, proxy_query_endpoint(), proxy_query_stream_endpoint() (+15 more)

### Community 9 - "AI Engine Query Router & Contracts"
Cohesion: 0.14
Nodes (21): EngineQueryRequest, HistoryMessage, Any, BaseModel, post, StreamingResponse, query_endpoint(), query_stream_endpoint() (+13 more)

### Community 10 - "FastAPI Auth Dependencies & Current User"
Cohesion: 0.16
Nodes (16): FastAPI, get_current_user(), Session, Shared FastAPI dependencies: the authenticated user for protected routes., Resolves the bearer token to an existing user, or answers 401., get_meeting_service(), Session, Dependency provider for MeetingService, scoped to the logged-in account. (+8 more)

### Community 11 - "User Authentication & Registration"
Cohesion: 0.19
Nodes (18): login(), me(), get, post, Session, signup(), _token_for(), create_access_token() (+10 more)

### Community 12 - "Chat Graph Context Loader & Records"
Cohesion: 0.16
Nodes (19): date, load_context_node(), LangGraph Nodes for the Dedicated AI Engine Chatbot Pipeline. Implements the…, Step 1: Load the account's meetings, projects and speakers from PostgreSQL.…, build_catalog_records(), build_meeting_details(), format_meeting_date(), _meeting_day() (+11 more)

### Community 13 - "AI Engine Dynamic Configuration"
Cohesion: 0.18
Nodes (9): get_settings(), BaseSettings, Return cached singleton instance of Settings., Centralized Dynamic Configuration Module. Zero Hardcoding: All parameters are…, Settings, Internal authentication: the engine only answers calls that carry the shared…, Rejects any request that does not present the key shared with FS-backend., verify_internal_key() (+1 more)

### Community 14 - "Frontend Ingestion UI & Caching"
Cohesion: 0.18
Nodes (17): cache_data, render(), current_user_id(), Account id used to key cached data, so one account never sees another's cache., known_projects(), load_meeting(), load_meetings(), load_projects() (+9 more)

### Community 15 - "Structured Logging & Request Tracing"
Cohesion: 0.14
Nodes (11): _JsonFormatter, outbound_headers(), Central logging: one setup call, a request-ID context, JSON output for…, Headers to add to calls to other services so the request ID follows the call., Configure the root logger and route uvicorn's loggers through it. Safe to call…, Pure ASGI middleware: assigns the request ID and writes one structured access-…, _RequestIdFilter, RequestLoggingMiddleware (+3 more)

### Community 16 - "RAG Synthesis & Grounded Citations"
Cohesion: 0.16
Nodes (17): Grounded Citation Synthesis, Query Rewrite and Intent Analysis, Vector-Keyword Hybrid Turn Scoring, Architecture Notion Overview, _finish(), _initial_state(), Any, FS-AI-Engine/graphs/chat_graph.py ================================= Compiled… (+9 more)

### Community 17 - "Topic Segmentation & Heading Generation"
Cohesion: 0.15
Nodes (11): get_topic_segmenter(), Asynchronously segment turns into cohesive Level 2 topic segments with LLM-…, Construct a validated TopicSegment with async LLM heading generation., Synthesize an understandable, executive-level 5-to-6 word topic heading…, Generate a meaningful 5-to-6 word descriptive heading without vague 'X & Y & Z'…, Synthesize an informative summary for the topic segment., Level 2 Hybrid Boundary Topic Segmenter. 1. Computes similarity guardrails…, Tokenize text into lowercase alphanumeric tokens excluding stopwords. (+3 more)

### Community 18 - "Speaker Turn Normalization & Deep Dive"
Cohesion: 0.15
Nodes (13): Lexical Topic Segmentation, Multi-Speaker Turn Normalization, Project Deep Dive and Speaking Notes, get_normalizer(), Raw sentence representation as returned by Fireflies GraphQL API., RawSentence, Speaker Normalization Engine for Meeting Transcripts. Responsibilities: 1.…, Construct a validated SpeakerTurn from a sequence of sentences by one speaker. (+5 more)

### Community 19 - "Fireflies Ingestion Background Task"
Cohesion: 0.18
Nodes (14): _process_fireflies_dashboard_task(), Background worker task to fetch transcript from Fireflies and orchestrate…, MeetingTopicModel, Tier 3: Level 2 Meeting Topic Segments., PipelineOrchestratorError, Exception raised when background ingestion orchestration fails., BackendPipelineOrchestrator, _fmt_ts() (+6 more)

### Community 20 - "Frontend Meeting Explorer & Detail View"
Cohesion: 0.18
Nodes (16): _detail(), Meetings page: library on the left, transcript detail on the right., render(), chat_to_markdown(), format_meeting_date(), format_timestamp(), meeting_label(), parse_meeting_id() (+8 more)

### Community 21 - "Backend Configuration & CORS Settings"
Cohesion: 0.15
Nodes (8): BaseSettings, field_validator, Backend system configuration loaded from environment or .env file., Settings, FirefliesClient, get_fireflies_client(), Dedicated Asynchronous Fireflies.ai GraphQL Client for FS-backend. Handles: 1.…, Session

### Community 22 - "Streamlit Chat Assistant & History"
Cohesion: 0.18
Nodes (14): _ask(), _ask_streaming(), build_chat_history(), Assistant page: grounded Q&A over transcripts., The most recent `max_questions` user questions with the answers that followed…, Writes the answer into the chat as it arrives and returns the finished message,…, render(), _render() (+6 more)

### Community 23 - "Frontend Authentication & Session State"
Cohesion: 0.18
Nodes (14): current_user(), _login_form(), logout(), Any, Login / sign-up page and the logged-in session (kept per browser session in…, Shown instead of the app until the user is logged in., Who is logged in, with a log-out button (sidebar)., render_account_menu() (+6 more)

### Community 24 - "Link Parsing & Ingestion Routing"
Cohesion: 0.14
Nodes (16): BackgroundTasks, _assert_ids_available(), ingest_meeting_link(), IngestLinkRequest, IngestLinkResponse, parse_meeting_link(), _process_raw_media_task(), Any (+8 more)

### Community 25 - "Query Routing & LLM Analysis"
Cohesion: 0.21
Nodes (13): analyze_query(), _clean(), Any, QueryAnalysis, FS-AI-Engine/graphs/chat_analysis.py ==================================== The…, Structured routing decision. The defaults describe the safe path: full…, Classifies the question and extracts its entities. Anything the model returns…, is_name_token_match() (+5 more)

### Community 26 - "Frontend UI Components & Styling"
Cohesion: 0.22
Nodes (15): highlight(), _inject_popover_script(), message_pills(), page_header(), Any, Reusable presentational components., Escape `text` and wrap case-insensitive matches of `needle` in <mark>., Stable colour per speaker name. (+7 more)

### Community 27 - "Conversational Query Rewriting"
Cohesion: 0.18
Nodes (9): AsyncOpenAI, FS-AI-Engine/graphs/chat_rewrite.py =================================== The…, Returns the standalone, typo-corrected question. Falls back to the original…, rewrite_query(), get_llm_client(), LLMClient, Centralized Asynchronous LLM Client Utility. Serves as the universal LLM…, Dependency injector for LLMClient. (+1 more)

### Community 28 - "Fireflies GraphQL API Client"
Cohesion: 0.18
Nodes (10): FirefliesAPIError, Any, Exception, Executes a GraphQL query or mutation with exponential backoff retries., Query Fireflies GraphQL endpoint for a complete meeting transcript. Args:…, Submit an external raw media recording URL (mp3, wav, mp4, m4a) to Fireflies…, Exception raised when Fireflies API call or GraphQL operation fails., Save raw Fireflies transcript dictionary as a JSON file in the root… (+2 more)

### Community 29 - "Hybrid Vector & Keyword Search"
Cohesion: 0.17
Nodes (13): SQL fragment matching a speaker (metadata or column) against a LIKE parameter., _speaker_clause(), embed_query_node(), enrich_chunk_text(), _name_tokens_in(), Any, Prefixes a transcript chunk with speaker and timestamp metadata for…, True if any 3+ letter token of `target` occurs inside the lowercased speaker… (+5 more)

### Community 30 - "Gemini Embedding Generation Service"
Cohesion: 0.23
Nodes (8): get_embedding_service(), EmbeddingService, EmbeddingServiceError, Exception, Generate deterministic, normalized pseudo-vectors of dimension…, Custom exception raised when embedding generation fails., Dynamic Embedding Service routed through Google Gemini universal provider. Zero…, Asynchronously generate high-dimensional vector embeddings for a list of text…

### Community 31 - "Citation Formatting & Excerpt Alignment"
Cohesion: 0.29
Nodes (10): align_citations_with_answer(), build_citations(), format_citations_node(), _is_trivial_filler(), Any, FS-AI-Engine/graphs/chat_citations.py =====================================…, Node 5: Format Citations with Provenance Alignment & Parity Audit. Aligns…, Provenance Alignment: Guarantees that citation cards strictly align with facts,… (+2 more)

### Community 32 - "Frontend App Entry & Overview Dashboard"
Cohesion: 0.20
Nodes (7): Overview page: KPIs, project breakdown, recent meetings and a quick-ask box., render(), FS-frontend/app.py ================== Entry point for the Meeting Intelligence…, go(), Page registry so pages can link to each other via `go("assistant")`., Pre-set the assistant scope from another page., set_scope()

### Community 33 - "Schema-Comment Version Tracker"
Cohesion: 0.31
Nodes (8): _get_comment_heads(), _migrate_existing_table(), Any, PostgreSQL Schema-Comment-based version tracker for Alembic (Zero-Table…, Retrieve revision heads stored in the PostgreSQL schema comment., Update or clear the PostgreSQL schema comment with the active revision heads., If an alembic_version table exists, migrate its version to the comment and drop…, _sync_schema_comment()

### Community 34 - "LLM Completion Client & Streaming"
Cohesion: 0.29
Nodes (4): Sends asynchronous chat completion to Google Gemini. Provides robust…, The model name the Gemini endpoint accepts (provider prefixes stripped)., Streams the completion as text deltas. Provider order matches…, Generates natural, conversational, professional synthesis responses when…

### Community 35 - "Frontend Configuration & Secrets"
Cohesion: 0.25
Nodes (6): FrontendConfig, _populate_env_from_streamlit_secrets(), Any, BaseSettings, Centralized Frontend Configuration for the Streamlit Chat Application.…, Streamlit Cloud stores secrets in st.secrets, but does not inject them into…

### Community 36 - "Platform Architecture & Ingestion Principles"
Cohesion: 0.33
Nodes (7): Deterministic Turn IDs, 4-Tier Data Hierarchy, Ingestion Idempotency Hashing, LangGraph Chat Pipeline, Meeting Transcript Intelligence Platform, Per-Account Tenant Isolation, Architecture Documentation

### Community 37 - "Frontend Global CSS & Theme Styling"
Cohesion: 0.40
Nodes (5): inject_styles(), _load_css(), Global styling injection for Meeting Intelligence. Production CSS lives in…, Read and concatenate stylesheet parts from disk, in filename order., Inject global CSS into the current Streamlit app page.

### Community 38 - "Infrastructure Spec & DeepEval Benchmark"
Cohesion: 0.40
Nodes (5): DeepEval Golden Dataset Evaluation, Fireflies.ai GraphQL Transcript Integration, Gemini 2.5 Flash / Embeddings Integration, PostgreSQL pgvector HNSW Indexing, Technology Stack Spec

### Community 39 - "Baseline Database Schema Migration"
Cohesion: 0.40
Nodes (4): downgrade(), Drop all tables in reverse order., Create initial baseline schema across all 4 tiers., upgrade()

### Community 41 - "Multi-Tenant Owner Column Helper"
Cohesion: 0.67
Nodes (3): Column, _owner_column(), Owning account; every row of every data table belongs to exactly one user.

### Community 42 - "AI Engine Health Check"
Cohesion: 0.67
Nodes (3): health_check(), get, Health check endpoint for AI Engine.

## Knowledge Gaps
- **9 isolated node(s):** `fs-ai-engine`, `fs-backend`, `fs-frontend`, `Meeting Transcript Intelligence Platform`, `Deterministic Turn IDs` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 318 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **11 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TopicSegmenter` connect `Topic Segmentation & Heading Generation` to `Hierarchical Pipeline Schemas`, `AI Engine Ingestion Endpoints`, `AI Engine Dynamic Configuration`, `Speaker Turn Normalization & Deep Dive`, `Conversational Query Rewriting`, `Gemini Embedding Generation Service`?**
  _High betweenness centrality (0.044) - this node is a cross-community bridge._
- **Why does `LLMClient` connect `Conversational Query Rewriting` to `Topic Segmentation & Heading Generation`, `LLM Completion Client & Streaming`, `AI Engine Dynamic Configuration`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `SpeakerTurn` (e.g. with `SpeakerNormalizer` and `TopicSegmenter`) actually correct?**
  _`SpeakerTurn` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Are the 11 inferred relationships involving `ChatState` (e.g. with `format_citations_node()` and `analyze_query_node()`) actually correct?**
  _`ChatState` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `Settings` (e.g. with `get_embedding_service()` and `get_meeting_summarizer()`) actually correct?**
  _`Settings` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 4 inferred relationships involving `Settings` (e.g. with `get_meeting_service()` and `FirefliesClient`) actually correct?**
  _`Settings` has 4 INFERRED edges - model-reasoned connections that need verification._
- **What connects `fs-ai-engine`, `fs-backend`, `fs-frontend` to the rest of the system?**
  _9 weakly-connected nodes found - possible documentation gaps or missing edges._