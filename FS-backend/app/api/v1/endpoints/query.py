"""
FS-backend/app/api/v1/endpoints/query.py
=========================================
Transparent API Gateway Proxy for Query Routing.
Relays incoming client requests directly to FS-AI-Engine (:8001) via HTTP REST.
Contains ZERO AI orchestration, embedding generation, pgvector search, or LLM logs.
"""

import contextlib
import json
import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core.config import get_settings
from ....core.database import UserModel, get_db
from ....services.ownership import resolve_project_id

logger = logging.getLogger("api-gateway")

router = APIRouter()
settings = get_settings()


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=8000)


class QueryRequest(BaseModel):
    query: str = Field(..., description="Natural language question or search query")
    project_id: str | None = Field(default=None, description="Optional project boundary filter")
    meeting_id: str | None = Field(default=None, description="Optional meeting filter")
    speaker: str | None = Field(default=None, description="Optional speaker filter")
    history: list[HistoryMessage] = Field(
        default_factory=list, max_length=50, description="Recent conversation turns, oldest first (trimmed by the client)"
    )


class Citation(BaseModel):
    speaker: str
    start_time: float
    end_time: float
    meeting_id: str
    exact_raw_text: str
    topic_context: str | None = "Level 1 Turn"
    citation_type: str = "turn"  # "turn" (quoted turn) or "meeting" (meeting + timestamp only)
    meeting_title: str | None = None


class QueryResponse(BaseModel):
    query: str
    routing_mode: str
    routing_reasoning: str
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    citations_count: int = 0
    provenance_verified: bool = True
    macro_data: dict[str, Any] | None = None
    llm_model_used: str = "gemini-2.5-flash"


def _engine_url(path: str) -> str:
    ai_base = (settings.ai_engine_url or "").rstrip("/")
    ai_base = ai_base.removesuffix("/api/v1")
    return f"{ai_base}/api/v1/engine/{path}"


def _engine_payload(request: QueryRequest, user: UserModel, db: Session) -> dict[str, Any]:
    scoped_project_id = resolve_project_id(db, user.id, request.project_id) if request.project_id else None
    return {
        "query": request.query,
        "project_id": scoped_project_id,
        "meeting_id": request.meeting_id,
        "speaker": request.speaker,
        "history": [m.model_dump() for m in request.history],
        "user_id": user.id,  # always the authenticated account, never client-supplied
    }


def _engine_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "X-Internal-Api-Key": settings.engine_api_key}


def _engine_http_error(resp: httpx.Response) -> HTTPException:
    """The error to return to the client for a non-200 answer from the engine (the body must already be read)."""
    if resp.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        # The user is logged in; the backend itself was refused. Not a 401, or the UI would log the user out.
        logger.error("[API Gateway] AI Engine rejected the internal API key (check ENGINE_API_KEY in both .env files)")
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The AI engine rejected this server's credentials. Check ENGINE_API_KEY configuration.",
        )
    logger.warning(f"[API Gateway] AI Engine returned HTTP {resp.status_code}")
    error_detail = resp.text
    with contextlib.suppress(Exception):
        error_detail = resp.json().get("detail", resp.text)
    return HTTPException(status_code=resp.status_code, detail=error_detail)


@router.post(
    "/query",
    response_model=QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Transparent Proxy Gateway to FS-AI-Engine",
    description="Forwards client queries directly to FS-AI-Engine (/api/v1/engine/query) with zero local RAG processing.",
)
async def proxy_query_endpoint(
    request: QueryRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QueryResponse:
    """
    Pure Pass-Through Proxy:
    Forwards payload to FS-AI-Engine without any local vector search or OpenRouter calls.
    Logs ONLY basic HTTP proxy transmission status.
    """
    target_endpoint = _engine_url("query")
    logger.info(f"[API Gateway] Proxying query request -> {target_endpoint}")

    payload = _engine_payload(request, user, db)

    try:
        async with httpx.AsyncClient(timeout=settings.ai_engine_timeout_seconds) as client:
            resp = await client.post(target_endpoint, json=payload, headers=_engine_headers())

        if resp.status_code == 200:
            logger.info(f"[API Gateway] AI Engine response received (HTTP {resp.status_code})")
            return QueryResponse(**resp.json())
        raise _engine_http_error(resp)

    except httpx.ConnectError as err:
        logger.error(f"[API Gateway] Connection Refused: Could not reach FS-AI-Engine at {target_endpoint}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"FS-AI-Engine unavailable at {target_endpoint}. Verify AI_ENGINE_URL environment variable."
        ) from err
    except httpx.TimeoutException as err:
        logger.error("[API Gateway] Request timed out while waiting for FS-AI-Engine response")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI Engine query processing timed out."
        ) from err
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[API Gateway] Unexpected proxy error: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gateway proxy error. See the backend logs for details."
        ) from exc


def _sse_error(message: str) -> bytes:
    return f"event: error\ndata: {json.dumps({'message': message})}\n\n".encode()


@router.post(
    "/query/stream",
    status_code=status.HTTP_200_OK,
    response_class=StreamingResponse,
    summary="Streaming proxy to FS-AI-Engine",
    description=(
        "Same request and account scoping as /query. The engine's server-sent events (`status`, `token`, `final`, "
        "`error`) are relayed as they arrive; `final` holds the complete /query response."
    ),
)
async def proxy_query_stream_endpoint(
    request: QueryRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Opens the engine's stream first (so engine errors still become proper HTTP errors), then relays its bytes."""
    target_endpoint = _engine_url("query/stream")
    logger.info(f"[API Gateway] Proxying streaming query request -> {target_endpoint}")

    payload = _engine_payload(request, user, db)
    client = httpx.AsyncClient(timeout=settings.ai_engine_timeout_seconds)  # the read timeout applies between chunks
    try:
        upstream = await client.send(
            client.build_request("POST", target_endpoint, json=payload, headers=_engine_headers()), stream=True
        )
        if upstream.status_code != 200:
            try:
                await upstream.aread()
            finally:
                await upstream.aclose()
            raise _engine_http_error(upstream)
    except httpx.ConnectError as err:
        await client.aclose()
        logger.error(f"[API Gateway] Connection Refused: Could not reach FS-AI-Engine at {target_endpoint}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"FS-AI-Engine unavailable at {target_endpoint}. Verify AI_ENGINE_URL environment variable.",
        ) from err
    except httpx.TimeoutException as err:
        await client.aclose()
        logger.error("[API Gateway] Request timed out while waiting for FS-AI-Engine response")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="AI Engine query processing timed out."
        ) from err
    except HTTPException:
        await client.aclose()
        raise
    except Exception as exc:
        await client.aclose()
        logger.error(f"[API Gateway] Unexpected proxy error: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Gateway proxy error. See the backend logs for details."
        ) from exc

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except httpx.HTTPError as exc:  # the engine went away or stalled after the stream had started
            logger.error(f"[API Gateway] Stream from FS-AI-Engine broke: {exc!r}")
            yield _sse_error("The AI engine stopped responding.")
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},  # no proxy buffering, so tokens arrive live
    )
