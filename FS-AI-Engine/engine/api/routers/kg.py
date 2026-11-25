"""
FS-AI-Engine/engine/api/routers/kg.py
=====================================
Knowledge graph router: POST /api/v1/kg/extract -> entities and relations of one meeting (persisted by FS-backend).
"""

import logging

from fastapi import APIRouter, HTTPException, status

from engine.services.kg_extractor import KGExtractRequest, KGExtractResponse, KnowledgeGraphExtractor

logger = logging.getLogger("fs-ai-engine")

router = APIRouter(prefix="/kg", tags=["knowledge-graph"])


@router.post(
    "/extract",
    response_model=KGExtractResponse,
    status_code=status.HTTP_200_OK,
    summary="Extract a meeting's knowledge graph",
    description=(
        "Builds the meeting's entities (people, projects, decisions, action items, blockers, technologies, topics) "
        "and relations, each grounded to Level 1 turn ids, with entity embeddings for cross-meeting resolution."
    ),
)
async def extract_endpoint(request: KGExtractRequest) -> KGExtractResponse:
    try:
        return await KnowledgeGraphExtractor().extract(request)
    except Exception as exc:
        logger.error(f"Knowledge graph extraction failed for '{request.meeting_id}': {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Knowledge graph extraction failed. See the AI engine logs for details.",
        ) from exc
