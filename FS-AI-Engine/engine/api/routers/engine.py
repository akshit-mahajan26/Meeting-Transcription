"""
FS-AI-Engine/engine/api/routers/engine.py
=========================================
Query router: POST /api/v1/engine/query -> LangGraph chat pipeline.
"""

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from engine.services.rag_service import RAGQueryResponse, process_user_query, stream_user_query

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
            detail="Query pipeline failed. See the AI engine logs for details.",
        ) from exc


def _sse(event: str, data: dict[str, Any]) -> str:
    """One server-sent event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post(
    "/query/stream",
    status_code=status.HTTP_200_OK,
    response_class=StreamingResponse,
    summary="Answer a question, streaming the answer as it is written",
    description=(
        "Same pipeline and request as /query, delivered as server-sent events: `status` ({message}) while the "
        "pipeline works, `token` ({text}) for each piece of the answer, then one `final` event holding the complete "
        "/query response (its `answer` is the canonical text and replaces what was streamed). A failure after the "
        "stream has started is sent as an `error` ({message}) event."
    ),
)
async def query_stream_endpoint(request: EngineQueryRequest) -> StreamingResponse:
    async def events():
        try:
            async for kind, payload in stream_user_query(
                query=request.query,
                user_id=request.user_id,
                project_id=request.project_id,
                meeting_id=request.meeting_id,
                speaker=request.speaker,
                history=[m.model_dump() for m in request.history],
            ):
                if kind == "final":
                    yield _sse("final", payload.model_dump())
                elif kind == "status":
                    yield _sse("status", {"message": payload})
                else:
                    yield _sse("token", {"text": payload})
        except Exception as exc:  # the 200 status is already sent, so the failure travels in the stream
            logger.error(f"Streaming query pipeline failed: {exc}", exc_info=True)
            yield _sse("error", {"message": "Query pipeline failed. See the AI engine logs for details."})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},  # no proxy buffering, so tokens arrive live
    )
