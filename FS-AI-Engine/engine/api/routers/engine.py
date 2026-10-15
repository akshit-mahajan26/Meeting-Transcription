"""
FS-AI-Engine/engine/api/routers/engine.py
=========================================
Query router: POST /api/v1/engine/query -> LangGraph chat pipeline.
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from engine.services.rag_service import RAGQueryResponse, process_user_query

logger = logging.getLogger("fs-ai-engine")

router = APIRouter(tags=["engine"])


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=8000)


class EngineQueryRequest(BaseModel):
    query: str = Field(..., description="Natural language query or question")
    user_id: str = Field(..., min_length=1, description="Authenticated account the query is scoped to (set by FS-backend)")
    project_id: str | None = Field(default=None, description="Optional project boundary scoping")
    meeting_id: str | None = Field(default=None, description="Optional meeting ID filter")
    speaker: str | None = Field(default=None, description="Optional speaker filter")
    history: list[HistoryMessage] = Field(default_factory=list, max_length=50, description="Recent conversation turns, oldest first")


@router.post(
    "/query",
    response_model=RAGQueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Answer a question with retrieval-augmented generation",
    description="Runs the LangGraph chat pipeline: catalog answers from the database, or embedding, pgvector retrieval and LLM synthesis with Level 1 citations.",
)
async def query_endpoint(request: EngineQueryRequest) -> RAGQueryResponse:
    try:
        return await process_user_query(
            query=request.query,
            user_id=request.user_id,
            project_id=request.project_id,
            meeting_id=request.meeting_id,
            speaker=request.speaker,
            history=[m.model_dump() for m in request.history],
        )
    except Exception as exc:
        logger.error(f"Query pipeline failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query pipeline failed: {exc!s}",
        ) from exc
