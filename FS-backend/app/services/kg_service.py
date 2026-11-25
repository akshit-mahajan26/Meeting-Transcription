"""Knowledge graph service: builds a meeting's graph through FS-AI-Engine and stores it in the KG database.

Write side
- build_for_meeting: reads the stored meeting (topics + turns), asks the engine to extract entities/relations,
  then replaces that meeting's slice of the account's graph.
- Entity resolution: an extracted entity joins an existing one with the same (type, normalized name); projects,
  technologies, organizations and topics also join a near-identical existing entity by embedding similarity.
  People are only ever merged by name, never by embedding (similar names are often different people).

Read side
- get_graph: nodes and aggregated edges for the graph view, optionally scoped to a project or meeting.
- get_entity: one entity with its connections, the meetings it appears in and quoted evidence turns.

The KG database has no foreign keys to the main database, so every query filters on user_id here.
"""

import asyncio
import logging
from collections import Counter
from typing import Any

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from ..core.config import Settings, get_settings
from ..core.database import MeetingModel, MeetingTopicModel, SessionLocal, SpeakerTurnModel
from ..core.kg_database import KGEntityModel, KGMentionModel, KGRelationModel, KGSessionLocal
from .ownership import unscope_project_id

logger = logging.getLogger(__name__)

RESOLVABLE_BY_EMBEDDING = ("project", "technology", "organization", "topic")
EMBEDDING_MERGE_SIMILARITY = 0.92
MAX_EVIDENCE_TURNS = 8


class KnowledgeGraphUnavailable(Exception):
    """Raised when KG_DATABASE_URL is not configured."""


def kg_enabled() -> bool:
    return KGSessionLocal is not None


class KnowledgeGraphService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        ai_base = (self.settings.ai_engine_url or "").rstrip("/").removesuffix("/api/v1")
        self.extract_url = f"{ai_base}/api/v1/kg/extract"
        self.engine_headers = {"X-Internal-Api-Key": self.settings.engine_api_key}

    # ------------------------------------------------------------------
    # Write side
    # ------------------------------------------------------------------

    @staticmethod
    def _extraction_payload(db: Session, user_id: str, meeting_id: str) -> tuple[dict[str, Any], str | None] | None:
        """The engine request for a stored meeting, and the meeting's project id (None if the meeting is unknown)."""
        meeting = db.query(MeetingModel).filter(MeetingModel.id == meeting_id, MeetingModel.user_id == user_id).first()
        if meeting is None:
            return None
        topics = (
            db.query(MeetingTopicModel)
            .filter(MeetingTopicModel.meeting_id == meeting_id, MeetingTopicModel.user_id == user_id)
            .order_by(MeetingTopicModel.topic_index)
            .all()
        )
        turns = (
            db.query(SpeakerTurnModel.turn_id, SpeakerTurnModel.topic_id, SpeakerTurnModel.speaker_name, SpeakerTurnModel.text)
            .filter(SpeakerTurnModel.meeting_id == meeting_id, SpeakerTurnModel.user_id == user_id)
            .order_by(SpeakerTurnModel.turn_number)
            .all()
        )
        by_topic: dict[str | None, list[dict[str, str]]] = {}
        for t in turns:
            by_topic.setdefault(t.topic_id, []).append({"turn_id": t.turn_id, "speaker": t.speaker_name, "text": t.text})
        segments = [{"title": top.title, "turns": by_topic.pop(top.id, [])} for top in topics]
        if by_topic.get(None):  # turns outside every topic window
            segments.append({"title": None, "turns": by_topic.pop(None)})
        segments = [s for s in segments if s["turns"]]

        project_name = meeting.project.name if meeting.project else unscope_project_id(meeting.project_id, user_id)
        payload = {
            "meeting_id": meeting_id,
            "title": meeting.title,
            "date": meeting.date,
            "project_name": project_name,
            "segments": segments,
        }
        return payload, meeting.project_id

    async def build_for_meeting(self, user_id: str, meeting_id: str) -> dict[str, Any]:
        """Extracts and stores one meeting's graph. Raises on failure (callers decide whether that is fatal)."""
        if not kg_enabled():
            raise KnowledgeGraphUnavailable("KG_DATABASE_URL is not configured.")

        db = SessionLocal()
        try:
            built = self._extraction_payload(db, user_id, meeting_id)
        finally:
            db.close()
        if built is None:
            raise ValueError(f"Meeting '{meeting_id}' not found for this account.")
        payload, project_id = built

        timeout = httpx.Timeout(connect=15.0, read=float(self.settings.ai_engine_timeout_seconds), write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(self.extract_url, json=payload, headers=self.engine_headers)
        resp.raise_for_status()
        graph = resp.json()
        if graph.get("llm_windows") and not graph.get("llm_segments"):
            # Every LLM call failed (provider down, quota): keep the meeting out of the graph so a rebuild retries it
            raise RuntimeError(f"Knowledge graph extraction failed for all {graph['llm_windows']} transcript windows.")

        stats = await asyncio.to_thread(self.persist, user_id, meeting_id, project_id, graph)
        logger.info(f"[KG] Stored graph of meeting '{meeting_id}': {stats}")
        return stats

    def persist(self, user_id: str, meeting_id: str, project_id: str | None, graph: dict[str, Any]) -> dict[str, int]:
        """Replaces the meeting's mentions and relations with `graph` (the engine's KGExtractResponse)."""
        kg = KGSessionLocal()
        try:
            kg.execute(delete(KGRelationModel).where(KGRelationModel.user_id == user_id, KGRelationModel.meeting_id == meeting_id))
            kg.execute(delete(KGMentionModel).where(KGMentionModel.user_id == user_id, KGMentionModel.meeting_id == meeting_id))

            key_to_id: dict[str, str] = {}
            mention_turns: dict[str, list[str]] = {}
            created = 0
            for ent in graph.get("entities") or []:
                entity, is_new = self._resolve_entity(kg, user_id, ent)
                created += is_new
                key_to_id[ent["key"]] = entity.id
                mention_turns[entity.id] = list(dict.fromkeys(mention_turns.get(entity.id, []) + (ent.get("turn_ids") or [])))

            for entity_id, turn_ids in mention_turns.items():
                kg.add(KGMentionModel(
                    user_id=user_id, entity_id=entity_id, meeting_id=meeting_id, project_id=project_id, turn_ids=turn_ids,
                ))

            seen: set[tuple[str, str, str]] = set()
            relations = 0
            for rel in graph.get("relations") or []:
                src, dst = key_to_id.get(rel.get("source")), key_to_id.get(rel.get("target"))
                rel_key = (src, dst, rel.get("relation_type"))
                if not src or not dst or src == dst or rel_key in seen:
                    continue
                seen.add(rel_key)
                relations += 1
                kg.add(KGRelationModel(
                    user_id=user_id, source_id=src, target_id=dst, relation_type=rel["relation_type"],
                    description=rel.get("description"), meeting_id=meeting_id, project_id=project_id,
                    turn_ids=rel.get("turn_ids") or [], confidence=float(rel.get("confidence") or 1.0),
                ))
            kg.flush()
            self._refresh_counts(kg, user_id)
            kg.commit()
            return {"entities": len(mention_turns), "new_entities": created, "relations": relations}
        except Exception:
            kg.rollback()
            raise
        finally:
            kg.close()

    @staticmethod
    def _resolve_entity(kg: Session, user_id: str, ent: dict[str, Any]) -> tuple[KGEntityModel, bool]:
        """The account's existing entity for `ent`, or a new one. Returns (entity, created)."""
        etype, norm, embedding = ent["entity_type"], ent["normalized_name"], ent.get("embedding")
        entity = kg.execute(
            select(KGEntityModel).where(
                KGEntityModel.user_id == user_id, KGEntityModel.entity_type == etype, KGEntityModel.normalized_name == norm
            )
        ).scalar_one_or_none()

        if entity is None and embedding and etype in RESOLVABLE_BY_EMBEDDING:
            distance = KGEntityModel.embedding.cosine_distance(embedding)
            row = kg.execute(
                select(KGEntityModel, distance.label("distance"))
                .where(KGEntityModel.user_id == user_id, KGEntityModel.entity_type == etype, KGEntityModel.embedding.isnot(None))
                .order_by(distance)
                .limit(1)
            ).first()
            if row is not None and 1.0 - float(row.distance) >= EMBEDDING_MERGE_SIMILARITY:
                entity = row[0]
                if ent["name"] not in (entity.aliases or []) and ent["name"] != entity.name:
                    entity.aliases = [*(entity.aliases or []), ent["name"]]

        if entity is not None:
            if not entity.description and ent.get("description"):
                entity.description = ent["description"]
            if entity.embedding is None and embedding:
                entity.embedding = embedding
            return entity, False

        entity = KGEntityModel(
            user_id=user_id, entity_type=etype, name=ent["name"], normalized_name=norm,
            description=ent.get("description"), aliases=[], embedding=embedding,
        )
        kg.add(entity)
        kg.flush()
        return entity, True

    @staticmethod
    def _refresh_counts(kg: Session, user_id: str) -> None:
        """Drops entities no meeting mentions any more and recounts mentions for the rest."""
        no_mentions = ~select(KGMentionModel.id).where(KGMentionModel.entity_id == KGEntityModel.id).exists()
        kg.execute(delete(KGEntityModel).where(KGEntityModel.user_id == user_id, no_mentions).execution_options(synchronize_session=False))
        mention_count = select(func.count(KGMentionModel.id)).where(KGMentionModel.entity_id == KGEntityModel.id).scalar_subquery()
        kg.execute(update(KGEntityModel).where(KGEntityModel.user_id == user_id).values(mention_count=mention_count).execution_options(synchronize_session=False))

    async def rebuild(self, user_id: str, meeting_ids: list[str]) -> None:
        """Background task: builds the graph of each meeting in turn; one failure does not stop the rest."""
        built = 0
        for meeting_id in meeting_ids:
            try:
                await self.build_for_meeting(user_id, meeting_id)
                built += 1
            except Exception as exc:
                logger.warning(f"[KG] Could not build the graph of meeting '{meeting_id}': {exc}")
        logger.info(f"[KG] Rebuild finished: {built}/{len(meeting_ids)} meetings.")

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    @staticmethod
    def _mention_scope(user_id: str, project_id: str | None, meeting_id: str | None):
        conditions = [KGMentionModel.user_id == user_id]
        if project_id:
            conditions.append(KGMentionModel.project_id == project_id)
        if meeting_id:
            conditions.append(KGMentionModel.meeting_id == meeting_id)
        return conditions

    def stats(self, kg: Session, db: Session, user_id: str) -> dict[str, Any]:
        meetings_in_graph = set(kg.execute(select(KGMentionModel.meeting_id).where(KGMentionModel.user_id == user_id).distinct()).scalars())
        processed = [
            m for (m,) in db.query(MeetingModel.id).filter(MeetingModel.user_id == user_id, MeetingModel.status == "PROCESSED")
        ]
        by_type = dict(kg.execute(
            select(KGEntityModel.entity_type, func.count()).where(KGEntityModel.user_id == user_id).group_by(KGEntityModel.entity_type)
        ).all())
        relations = kg.execute(select(func.count()).select_from(KGRelationModel).where(KGRelationModel.user_id == user_id)).scalar()
        return {
            "entities": sum(by_type.values()),
            "entities_by_type": by_type,
            "relations": relations or 0,
            "meetings_total": len(processed),
            "meetings_in_graph": len(meetings_in_graph & set(processed)),
            "meetings_missing": [m for m in processed if m not in meetings_in_graph],
        }

    def get_graph(
        self,
        kg: Session,
        user_id: str,
        project_id: str | None = None,
        meeting_id: str | None = None,
        entity_types: list[str] | None = None,
        limit: int = 150,
    ) -> dict[str, Any]:
        """The most-mentioned entities in scope (nodes) and the relations among them, merged per (source, target, type)."""
        scope = self._mention_scope(user_id, project_id, meeting_id)
        in_scope = (
            select(KGMentionModel.entity_id, func.count(KGMentionModel.id).label("meetings"))
            .where(*scope)
            .group_by(KGMentionModel.entity_id)
            .subquery()
        )
        node_query = (
            select(KGEntityModel, in_scope.c.meetings)
            .join(in_scope, in_scope.c.entity_id == KGEntityModel.id)
            .where(KGEntityModel.user_id == user_id)
        )
        if entity_types:
            node_query = node_query.where(KGEntityModel.entity_type.in_(entity_types))
        rows = kg.execute(node_query.order_by(in_scope.c.meetings.desc(), KGEntityModel.mention_count.desc()).limit(limit)).all()
        nodes = {
            e.id: {
                "id": e.id, "label": e.name, "type": e.entity_type, "description": e.description,
                "meetings": int(meetings), "mentions": e.mention_count,
            }
            for e, meetings in rows
        }

        edges: list[dict[str, Any]] = []
        if nodes:
            rel_conditions = [
                KGRelationModel.user_id == user_id,
                KGRelationModel.source_id.in_(nodes),
                KGRelationModel.target_id.in_(nodes),
            ]
            if project_id:
                rel_conditions.append(KGRelationModel.project_id == project_id)
            if meeting_id:
                rel_conditions.append(KGRelationModel.meeting_id == meeting_id)
            grouped = kg.execute(
                select(
                    KGRelationModel.source_id, KGRelationModel.target_id, KGRelationModel.relation_type,
                    func.count(KGRelationModel.id).label("weight"),
                )
                .where(*rel_conditions)
                .group_by(KGRelationModel.source_id, KGRelationModel.target_id, KGRelationModel.relation_type)
            ).all()
            edges = [
                {"source": r.source_id, "target": r.target_id, "type": r.relation_type, "weight": int(r.weight)}
                for r in grouped
            ]
        return {"nodes": list(nodes.values()), "edges": edges}

    def get_entity(self, kg: Session, db: Session, user_id: str, entity_id: str) -> dict[str, Any] | None:
        entity = kg.execute(
            select(KGEntityModel).where(KGEntityModel.id == entity_id, KGEntityModel.user_id == user_id)
        ).scalar_one_or_none()
        if entity is None:
            return None

        mentions = kg.execute(
            select(KGMentionModel).where(KGMentionModel.entity_id == entity_id, KGMentionModel.user_id == user_id)
        ).scalars().all()
        relations = kg.execute(
            select(KGRelationModel).where(
                KGRelationModel.user_id == user_id,
                or_(KGRelationModel.source_id == entity_id, KGRelationModel.target_id == entity_id),
            )
        ).scalars().all()
        other_ids = {r.target_id if r.source_id == entity_id else r.source_id for r in relations}
        others = {
            e.id: e for e in kg.execute(select(KGEntityModel).where(KGEntityModel.id.in_(other_ids), KGEntityModel.user_id == user_id)).scalars()
        } if other_ids else {}

        meeting_ids = {m.meeting_id for m in mentions} | {r.meeting_id for r in relations}
        titles = dict(
            db.query(MeetingModel.id, MeetingModel.title).filter(MeetingModel.user_id == user_id, MeetingModel.id.in_(meeting_ids)).all()
        ) if meeting_ids else {}

        connections = []
        for r in relations:
            outgoing = r.source_id == entity_id
            other = others.get(r.target_id if outgoing else r.source_id)
            if other is None:
                continue
            connections.append({
                "direction": "out" if outgoing else "in",
                "relation_type": r.relation_type,
                "entity": {"id": other.id, "label": other.name, "type": other.entity_type},
                "description": r.description,
                "meeting_id": r.meeting_id,
                "meeting_title": titles.get(r.meeting_id) or r.meeting_id,
            })

        # Quoted evidence: the turns most often cited for this entity across its mentions and relations
        turn_counts = Counter(t for m in mentions for t in (m.turn_ids or []))
        turn_counts.update(t for r in relations for t in (r.turn_ids or []))
        top_turns = [t for t, _ in turn_counts.most_common(MAX_EVIDENCE_TURNS)]
        evidence = []
        if top_turns:
            rows = (
                db.query(SpeakerTurnModel.turn_id, SpeakerTurnModel.meeting_id, SpeakerTurnModel.speaker_name,
                         SpeakerTurnModel.text, SpeakerTurnModel.start_time)
                .filter(SpeakerTurnModel.user_id == user_id, SpeakerTurnModel.turn_id.in_(top_turns))
                .all()
            )
            order = {t: i for i, t in enumerate(top_turns)}
            evidence = [
                {"turn_id": r.turn_id, "meeting_id": r.meeting_id, "meeting_title": titles.get(r.meeting_id) or r.meeting_id,
                 "speaker": r.speaker_name, "text": r.text, "start_time": r.start_time}
                for r in sorted(rows, key=lambda r: order[r.turn_id])
            ]

        return {
            "id": entity.id,
            "label": entity.name,
            "type": entity.entity_type,
            "description": entity.description,
            "aliases": entity.aliases or [],
            "mentions": entity.mention_count,
            "meetings": [
                {"meeting_id": m.meeting_id, "meeting_title": titles.get(m.meeting_id) or m.meeting_id,
                 "project_id": unscope_project_id(m.project_id, user_id)}
                for m in mentions
            ],
            "connections": connections,
            "evidence": evidence,
        }


knowledge_graph_service = KnowledgeGraphService()


def get_knowledge_graph_service() -> KnowledgeGraphService:
    return knowledge_graph_service
