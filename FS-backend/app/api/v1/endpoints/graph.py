"""Knowledge graph endpoints: the account's entity graph, entity details, and (re)building it from stored meetings."""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ....api.deps import get_current_user
from ....core.database import MeetingModel, UserModel, get_db
from ....core.kg_database import get_kg_db
from ....services.kg_service import KnowledgeGraphService, get_knowledge_graph_service
from ....services.ownership import resolve_project_id

router = APIRouter()


class GraphRebuildRequest(BaseModel):
    meeting_ids: list[str] | None = Field(default=None, description="Meetings to (re)build; omit for the whole library")
    only_missing: bool = Field(default=True, description="Skip meetings that already have a graph")


class GraphRebuildResponse(BaseModel):
    status: str
    queued: int


def _require_kg(kg: Session | None) -> Session:
    if kg is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The knowledge graph is not configured (KG_DATABASE_URL is not set).",
        )
    return kg


@router.get("/stats", summary="Knowledge graph size and coverage")
def graph_stats(
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
    kg: Session | None = Depends(get_kg_db),
    service: KnowledgeGraphService = Depends(get_knowledge_graph_service),
) -> dict[str, Any]:
    return service.stats(_require_kg(kg), db, user.id)


@router.get("", summary="Entity graph (nodes and edges) for the account, a project or a meeting")
def get_graph(
    project_id: str | None = Query(default=None),
    meeting_id: str | None = Query(default=None),
    types: list[str] | None = Query(default=None, description="Entity types to include"),
    limit: int = Query(default=150, ge=1, le=500),
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
    kg: Session | None = Depends(get_kg_db),
    service: KnowledgeGraphService = Depends(get_knowledge_graph_service),
) -> dict[str, Any]:
    scoped_project_id = resolve_project_id(db, user.id, project_id) if project_id else None
    return service.get_graph(_require_kg(kg), user.id, scoped_project_id, meeting_id, types, limit)


@router.get("/entities/{entity_id}", summary="One entity with its connections, meetings and evidence")
def get_entity(
    entity_id: str,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
    kg: Session | None = Depends(get_kg_db),
    service: KnowledgeGraphService = Depends(get_knowledge_graph_service),
) -> dict[str, Any]:
    entity = service.get_entity(_require_kg(kg), db, user.id, entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found.")
    return entity


@router.post(
    "/rebuild",
    response_model=GraphRebuildResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Build the graph of already-ingested meetings in the background",
)
def rebuild_graph(
    payload: GraphRebuildRequest,
    background_tasks: BackgroundTasks,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
    kg: Session | None = Depends(get_kg_db),
    service: KnowledgeGraphService = Depends(get_knowledge_graph_service),
) -> GraphRebuildResponse:
    stats = service.stats(_require_kg(kg), db, user.id)
    candidates = stats["meetings_missing"] if payload.only_missing else None
    if candidates is None:
        candidates = [m for (m,) in db.query(MeetingModel.id).filter(MeetingModel.user_id == user.id, MeetingModel.status == "PROCESSED")]
    if payload.meeting_ids is not None:
        wanted = set(payload.meeting_ids)
        candidates = [m for m in candidates if m in wanted]
    if candidates:
        background_tasks.add_task(service.rebuild, user.id, candidates)
    return GraphRebuildResponse(status="queued" if candidates else "nothing_to_do", queued=len(candidates))
