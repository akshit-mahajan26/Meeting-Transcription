"""
FS-backend/app/api/routers/query.py
====================================
Transparent API Gateway Proxy for Query Routing.
Relays incoming client requests directly to FS-AI-Engine (:8001) via HTTP REST.
Contains ZERO AI orchestration, embedding generation, pgvector search, or LLM logs.
"""

import logging
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
import httpx

from ...core.config import get_settings

logger = logging.getLogger("api-gateway")

router = APIRouter(tags=["query"])
settings = get_settings()

AI_ENGINE_BASE_URL = getattr(settings, "ai_engine_url", None) or getattr(settings, "AI_ENGINE_URL", "http://localhost:8001")
AI_ENGINE_BASE_URL = str(AI_ENGINE_BASE_URL).rstrip("/")
AI_ENGINE_QUERY_ENDPOINT = f"{AI_ENGINE_BASE_URL}/api/v1/engine/query"


class QueryRequest(BaseModel):
    query: str = Field(..., description="Natural language question or search query")
    project_id: Optional[str] = Field(default=None, description="Optional project boundary filter")
    meeting_id: Optional[str] = Field(default=None, description="Optional meeting filter")
    speaker: Optional[str] = Field(default=None, description="Optional speaker filter")


class Citation(BaseModel):
    speaker: str
    start_time: float
    end_time: float
    meeting_id: str
    exact_raw_text: str
    topic_context: Optional[str] = "Level 1 Turn"


class QueryResponse(BaseModel):
    query: str
    routing_mode: str
    routing_confidence: float
    routing_reasoning: str
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    citations_count: int = 0
    provenance_verified: bool = True
    macro_data: Optional[Dict[str, Any]] = None
    llm_model_used: str = "gemini-2.5-flash"


@router.post(
    "/query",
    response_model=QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Transparent Proxy Gateway to FS-AI-Engine",
    description="Forwards client queries directly to FS-AI-Engine (/api/v1/engine/query) with zero local RAG processing.",
)
async def proxy_query_endpoint(request: QueryRequest) -> QueryResponse:
    """
    Pure Pass-Through Proxy:
    Forwards payload to FS-AI-Engine without any local vector search or OpenRouter calls.
    Logs ONLY basic HTTP proxy transmission status.
    """
    logger.info(f"[API Gateway] Proxying query request -> {AI_ENGINE_QUERY_ENDPOINT}")

    payload = {
        "query": request.query,
        "project_id": request.project_id,
        "meeting_id": request.meeting_id,
        "speaker": request.speaker,
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                AI_ENGINE_QUERY_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json"}
            )

        if resp.status_code == 200:
            logger.info(f"[API Gateway] AI Engine response received (HTTP {resp.status_code})")
            return QueryResponse(**resp.json())
        else:
            logger.warning(f"[API Gateway] AI Engine returned HTTP {resp.status_code}")
            error_detail = resp.text
            try:
                error_detail = resp.json().get("detail", resp.text)
            except Exception:
                pass
            raise HTTPException(
                status_code=resp.status_code,
                detail=error_detail
            )

    except httpx.ConnectError:
        logger.error(f"[API Gateway] Connection Refused: Could not reach FS-AI-Engine at {AI_ENGINE_QUERY_ENDPOINT}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"FS-AI-Engine unavailable at {AI_ENGINE_QUERY_ENDPOINT}. Ensure FS-AI-Engine is running on port 8001."
        )
    except httpx.TimeoutException:
        logger.error(f"[API Gateway] Request timed out while waiting for FS-AI-Engine response")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI Engine query processing timed out."
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[API Gateway] Unexpected proxy error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gateway proxy error: {str(exc)}"
        )
