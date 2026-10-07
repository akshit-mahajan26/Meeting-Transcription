"""
FS-AI-Engine/api/routers/engine.py
===================================
Dedicated AI Engine Query & RAG Orchestration Router.
Routes client queries directly into the Universal RAG Pipeline (rag_service.py).
"""

import sys
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from fastapi import APIRouter, status

CURRENT_DIR = Path(__file__).resolve().parent
AI_ENGINE_ROOT = CURRENT_DIR.parent.parent
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

try:
    from services.rag_service import (
        process_user_query,
        CitationModel,
        RAGQueryResponse,
        UNIVERSAL_RAG_SYSTEM_PROMPT,
    )
except ModuleNotFoundError:
    from engine.services.rag_service import (
        process_user_query,
        CitationModel,
        RAGQueryResponse,
        UNIVERSAL_RAG_SYSTEM_PROMPT,
    )

logger = logging.getLogger("fs-ai-engine")

router = APIRouter(tags=["engine"])


class EngineQueryRequest(BaseModel):
    query: str = Field(..., description="Natural language query or question")
    project_id: Optional[str] = Field(default=None, description="Optional project boundary scoping")
    meeting_id: Optional[str] = Field(default=None, description="Optional meeting ID filter")
    speaker: Optional[str] = Field(default=None, description="Optional speaker filter")


@router.post(
    "/query",
    response_model=RAGQueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Universal Vector RAG & OpenRouter LLM Synthesis",
    description="Processes any user query through embedding generation, pgvector retrieval, and OpenRouter LLM completion with Level 1 citations.",
)
async def query_endpoint(request: EngineQueryRequest) -> RAGQueryResponse:
    return await process_user_query(
        query=request.query,
        project_id=request.project_id,
        meeting_id=request.meeting_id,
        speaker=request.speaker,
    )


@router.post(
    "/engine/query",
    response_model=RAGQueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Universal Vector RAG & OpenRouter LLM Synthesis (Explicit Route)",
    description="Processes any user query through embedding generation, pgvector retrieval, and OpenRouter LLM completion with Level 1 citations.",
)
async def query_explicit_endpoint(request: EngineQueryRequest) -> RAGQueryResponse:
    return await process_user_query(
        query=request.query,
        project_id=request.project_id,
        meeting_id=request.meeting_id,
        speaker=request.speaker,
    )
