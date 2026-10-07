import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..core.config import settings
from ..core.database import db_manager
from ..models.db_models import Project, Meeting, MeetingTopic
from .agent_router import RoutingDecision

logger = logging.getLogger(__name__)


class ProjectHealthRecord(BaseModel):
    """Structured deterministic project status and health metrics."""
    project_id: str
    name: str
    status: str
    health_score: float
    description: Optional[str] = None
    total_meetings: int
    created_at: Optional[str] = None


class MeetingSummaryRecord(BaseModel):
    """Structured Tier 3 executive summary and topic overview for a meeting."""
    meeting_id: str
    title: str
    meeting_date: Optional[str] = None
    executive_summary: Optional[str] = None
    status: str
    topics: List[Dict[str, Any]] = Field(default_factory=list)


class MacroRetrievalResult(BaseModel):
    """Zero-Hallucination Macro Retrieval Package."""
    query: str
    project_id: Optional[str] = None
    projects: List[ProjectHealthRecord] = Field(default_factory=list)
    meetings: List[MeetingSummaryRecord] = Field(default_factory=list)
    total_records: int
    retrieval_mode: str = "MACRO_SQL_DETERMINISTIC"


class MacroRetriever:
    """
    Relational SQL Macro Service.
    Executes deterministic asynchronous SQL queries across projects, meetings,
    and topics to produce verifiable macro-level intelligence with zero vector search hallucination.
    """

    def __init__(self, limit: Optional[int] = None):
        self.limit = limit or settings.MACRO_SEARCH_LIMIT

    async def execute_macro_retrieval(self, decision: RoutingDecision) -> MacroRetrievalResult:
        """
        Executes macro SQL joins based on routing filters.
        """
        project_id = decision.filters.project_id
        logger.info(f"[MacroRetriever] Executing deterministic macro SQL for project_id='{project_id}'")

        projects_data = await self.get_project_health(project_id)
        meetings_data = await self.get_executive_summaries(project_id, limit=self.limit)

        total_records = len(projects_data) + len(meetings_data)
        logger.info(f"[MacroRetriever] Fetched {len(projects_data)} projects and {len(meetings_data)} meetings via SQL.")

        return MacroRetrievalResult(
            query=decision.query,
            project_id=project_id,
            projects=projects_data,
            meetings=meetings_data,
            total_records=total_records
        )

    async def get_project_health(self, project_id: Optional[str] = None) -> List[ProjectHealthRecord]:
        """
        Queries project status, health score, and total meeting count via relational SQL.
        """
        async with db_manager.get_session() as session:
            if session is not None:
                try:
                    stmt = select(Project)
                    if project_id:
                        stmt = stmt.where(Project.id == project_id)
                    res = await session.execute(stmt)
                    projects = res.scalars().all()

                    records = []
                    for p in projects:
                        stmt_mtg = select(Meeting).where(Meeting.project_id == p.id)
                        res_mtg = await session.execute(stmt_mtg)
                        mtgs = res_mtg.scalars().all()

                        records.append(ProjectHealthRecord(
                            project_id=p.id,
                            name=p.name,
                            status=getattr(p, "status", "active"),
                            health_score=getattr(p, "health_score", 1.0) or 1.0,
                            description=p.description,
                            total_meetings=len(mtgs),
                            created_at=str(p.created_at) if p.created_at else None
                        ))
                    return records
                except Exception as e:
                    logger.warning(f"Live SQL execution failed ({e}). Checking in-memory store...")

        # In-memory store fallback
        mem = db_manager.get_in_memory_records()
        records = []
        for p_id, p_data in mem["projects"].items():
            if project_id and p_id != project_id:
                continue
            total_mtgs = sum(1 for m in mem["meetings"].values() if m.get("project_id") == p_id)
            records.append(ProjectHealthRecord(
                project_id=p_id,
                name=p_data.get("name", p_id),
                status=p_data.get("status", "active"),
                health_score=p_data.get("health_score", 1.0),
                description=p_data.get("description"),
                total_meetings=total_mtgs,
                created_at=str(p_data.get("created_at"))
            ))
        return records

    async def get_executive_summaries(
        self,
        project_id: Optional[str] = None,
        limit: int = 10
    ) -> List[MeetingSummaryRecord]:
        """
        Queries Tier 3 executive summaries and Level 2 topic metadata across meetings.
        """
        async with db_manager.get_session() as session:
            if session is not None:
                try:
                    stmt = select(Meeting).order_by(Meeting.date.desc()).limit(limit)
                    if project_id:
                        stmt = stmt.where(Meeting.project_id == project_id)
                    res = await session.execute(stmt)
                    meetings = res.scalars().all()

                    records = []
                    for m in meetings:
                        stmt_top = select(MeetingTopic).where(MeetingTopic.meeting_id == m.id)
                        res_top = await session.execute(stmt_top)
                        topics = res_top.scalars().all()
                        topics_list = [
                            {"topic_name": t.title, "summary": t.compiled_text}
                            for t in topics
                        ]

                        records.append(MeetingSummaryRecord(
                            meeting_id=m.id,
                            title=m.title or m.id,
                            meeting_date=str(m.date or m.created_at),
                            executive_summary=m.summary,
                            status=m.status,
                            topics=topics_list
                        ))
                    return records
                except Exception as e:
                    logger.warning(f"Live SQL meeting summaries query failed ({e}). Checking in-memory store...")

        # In-memory store fallback
        mem = db_manager.get_in_memory_records()
        records = []
        for m_id, m_data in mem["meetings"].items():
            if project_id and m_data.get("project_id") != project_id:
                continue
            topics_list = [
                {"topic_name": t.get("topic_name"), "summary": t.get("summary")}
                for t in mem["topics"].values()
                if t.get("meeting_id") == m_id
            ]
            records.append(MeetingSummaryRecord(
                meeting_id=m_id,
                title=m_data.get("title", m_id),
                meeting_date=str(m_data.get("meeting_date")),
                executive_summary=m_data.get("executive_summary"),
                status=m_data.get("status", "processed"),
                topics=topics_list
            ))
            if len(records) >= limit:
                break
        return records
