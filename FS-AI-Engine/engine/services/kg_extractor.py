"""
FS-AI-Engine/services/kg_extractor.py
=====================================
Knowledge graph extraction: turns a meeting's topic segments into entities and relations.

- A deterministic skeleton (the meeting, its project, its speakers and its topic segments) is always built.
- One LLM call per topic segment (long segments are split) adds people, projects, decisions, action items,
  blockers, technologies, organizations and topics, plus the relations between them. Every entity and relation
  is grounded to the Level 1 turn ids it came from; claims the LLM cannot ground are dropped.
- Entities are merged within the meeting by (type, normalized name) and embedded, so FS-backend can resolve them
  against the account's existing graph.

The result is returned to FS-backend, which owns the knowledge graph database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from .embedding_service import EmbeddingService
from .llm_client import get_llm_client

logger = logging.getLogger(__name__)

ENTITY_TYPES = (
    "person", "project", "decision", "action_item", "blocker", "technology", "topic", "organization", "meeting",
)
RELATION_TYPES = (
    "participated_in", "part_of", "discussed", "works_on", "owns", "assigned_to", "decided", "raised", "blocks",
    "depends_on", "uses", "mentions", "related_to",
)

SEGMENT_PROMPT_CHARS = 12_000  # transcript characters per extraction call
EXTRACTION_CONCURRENCY = 4
MAX_SKELETON_TURN_IDS = 20  # grounding turns kept on a speaker's participated_in edge

_GENERIC_TOPIC = re.compile(r"^(topic|segment|chapter)\s*\d+$|^meeting discussion", re.IGNORECASE)


# ============================================================================
# API models
# ============================================================================

class KGTurn(BaseModel):
    turn_id: str
    speaker: str
    text: str


class KGSegment(BaseModel):
    title: str | None = None
    turns: list[KGTurn] = Field(default_factory=list)


class KGExtractRequest(BaseModel):
    meeting_id: str
    title: str | None = None
    date: str | None = None
    project_name: str | None = None
    segments: list[KGSegment] = Field(default_factory=list)


class KGEntityOut(BaseModel):
    key: str  # "<type>:<normalized name>", unique within the response
    entity_type: str
    name: str
    normalized_name: str
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    turn_ids: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None


class KGRelationOut(BaseModel):
    source: str  # entity key
    target: str  # entity key
    relation_type: str
    description: str | None = None
    turn_ids: list[str] = Field(default_factory=list)
    confidence: float = 1.0


class KGExtractResponse(BaseModel):
    meeting_id: str
    entities: list[KGEntityOut] = Field(default_factory=list)
    relations: list[KGRelationOut] = Field(default_factory=list)
    llm_windows: int = 0  # extraction calls attempted (0 when no LLM is configured)
    llm_segments: int = 0  # extraction calls that returned usable JSON


# ============================================================================
# Helpers
# ============================================================================

def normalize_name(name: str) -> str:
    """Comparable form of an entity name: lowercase, single spaces, no surrounding punctuation or leading 'the'."""
    clean = re.sub(r"\s+", " ", (name or "").strip().lower())
    clean = clean.strip(" .,:;!?\"'`()[]{}")
    return clean.removeprefix("the ").strip()[:500]


def _entity_key(entity_type: str, name: str) -> str:
    return f"{entity_type}:{normalize_name(name)}"


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    """The first JSON object in an LLM reply (code fences and surrounding prose tolerated)."""
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _split_turns(turns: list[KGTurn]) -> list[list[KGTurn]]:
    """Splits a segment's turns into consecutive windows of at most SEGMENT_PROMPT_CHARS characters."""
    windows: list[list[KGTurn]] = [[]]
    size = 0
    for t in turns:
        line_len = len(t.text) + len(t.speaker) + 8
        if windows[-1] and size + line_len > SEGMENT_PROMPT_CHARS:
            windows.append([])
            size = 0
        windows[-1].append(t)
        size += line_len
    return [w for w in windows if w]


_SYSTEM_PROMPT = f"""You extract a knowledge graph from a meeting transcript excerpt.
Return ONLY a JSON object of the form:
{{"entities": [{{"name": str, "type": str, "description": str, "turns": [int]}}],
  "relations": [{{"source": str, "target": str, "type": str, "description": str, "turns": [int]}}]}}

Rules:
- Entity types: {", ".join(t for t in ENTITY_TYPES if t != "meeting")}.
- person: a named individual (use the fullest name used in the excerpt). Speakers are people.
- decision / action_item / blocker: a short statement (max 12 words) naming WHAT was decided, who must do what, or \
what is blocked, e.g. "Migrate billing to Stripe by Q3".
- technology: tools, systems, services, languages, vendors' products. organization: companies, teams, customers.
- topic: a substantive subject of discussion (not generic words like "update" or "meeting").
- Relation types: {", ".join(t for t in RELATION_TYPES if t not in ("participated_in", "part_of"))}.
  Use direction source -> target, e.g. person -assigned_to-> action_item, person -decided-> decision, \
blocker -blocks-> project, project -uses-> technology, person -works_on-> project.
- source and target must be names of entities in your "entities" list.
- "turns" lists the T-numbers of the excerpt lines that state the fact. Every entity and relation needs at least one.
- Extract only what the excerpt states. No speculation. Skip small talk. Empty lists are fine."""


# ============================================================================
# Extractor
# ============================================================================

class KnowledgeGraphExtractor:
    """Builds the knowledge graph fragment of one meeting."""

    def __init__(self, embedding_service: EmbeddingService | None = None):
        self.embedding_service = embedding_service or EmbeddingService()

    async def extract(self, request: KGExtractRequest) -> KGExtractResponse:
        entities: dict[str, KGEntityOut] = {}
        relations: dict[tuple[str, str, str], KGRelationOut] = {}

        def add_entity(entity_type: str, name: str, description: str | None, turn_ids: list[str]) -> str | None:
            norm = normalize_name(name)
            if not norm or entity_type not in ENTITY_TYPES:
                return None
            key = f"{entity_type}:{norm}"
            existing = entities.get(key)
            if existing is None:
                entities[key] = KGEntityOut(
                    key=key, entity_type=entity_type, name=name.strip()[:500], normalized_name=norm,
                    description=(description or "").strip() or None, turn_ids=list(dict.fromkeys(turn_ids)),
                )
            else:
                existing.turn_ids = list(dict.fromkeys(existing.turn_ids + turn_ids))
                if description and not existing.description:
                    existing.description = description.strip()
            return key

        def add_relation(source: str, target: str, rel_type: str, description: str | None, turn_ids: list[str], confidence: float) -> None:
            if source == target:
                return
            rel_key = (source, target, rel_type)
            existing = relations.get(rel_key)
            if existing is None:
                relations[rel_key] = KGRelationOut(
                    source=source, target=target, relation_type=rel_type,
                    description=(description or "").strip() or None, turn_ids=list(dict.fromkeys(turn_ids)), confidence=confidence,
                )
            else:
                existing.turn_ids = list(dict.fromkeys(existing.turn_ids + turn_ids))

        # 1. Deterministic skeleton: meeting, project, speakers, topic segments
        all_turns = [t for seg in request.segments for t in seg.turns]
        meeting_key = add_entity("meeting", request.title or request.meeting_id, request.date and f"Held on {request.date}", [])
        if request.project_name and meeting_key:
            project_key = add_entity("project", request.project_name, None, [])
            if project_key:
                add_relation(meeting_key, project_key, "part_of", None, [], 1.0)

        speaker_turns: dict[str, list[str]] = {}
        for t in all_turns:
            if t.speaker and t.speaker.strip().lower() not in ("unknown", "unknown speaker"):
                speaker_turns.setdefault(t.speaker.strip(), []).append(t.turn_id)
        speaker_keys: dict[str, str] = {}
        for speaker, turn_ids in speaker_turns.items():
            key = add_entity("person", speaker, None, turn_ids[:MAX_SKELETON_TURN_IDS])
            if key and meeting_key:
                speaker_keys[speaker] = key
                add_relation(key, meeting_key, "participated_in", None, turn_ids[:MAX_SKELETON_TURN_IDS], 1.0)

        for seg in request.segments:
            if seg.title and seg.turns and not _GENERIC_TOPIC.search(seg.title.strip()):
                key = add_entity("topic", seg.title, None, [t.turn_id for t in seg.turns[:5]])
                if key and meeting_key:
                    add_relation(meeting_key, key, "discussed", None, [seg.turns[0].turn_id], 1.0)

        # 2. LLM extraction per segment window
        windows = [w for seg in request.segments for w in _split_turns(seg.turns)]
        llm = get_llm_client()
        llm_ok = attempted = 0
        if windows and (llm.client is not None or llm._genai_client is not None):
            attempted = len(windows)
            sem = asyncio.Semaphore(EXTRACTION_CONCURRENCY)

            async def run(window: list[KGTurn]) -> tuple[list[KGTurn], dict[str, Any] | None]:
                async with sem:
                    return window, await self._extract_window(window, request.title)

            for window, parsed in await asyncio.gather(*(run(w) for w in windows)):
                if parsed is None:
                    continue
                llm_ok += 1
                self._merge_window(window, parsed, speaker_keys, add_entity, add_relation)
        else:
            logger.info(f"[KG] No LLM configured; meeting '{request.meeting_id}' gets the deterministic skeleton only.")

        # 3. Embed entities for cross-meeting resolution and query-time lookup
        entity_list = list(entities.values())
        try:
            vectors = await self.embedding_service.generate_embeddings(
                [f"{e.entity_type.replace('_', ' ')}: {e.name}. {e.description or ''}".strip() for e in entity_list]
            )
            for e, vec in zip(entity_list, vectors, strict=False):
                e.embedding = vec
        except Exception as exc:  # entities still resolve by exact name without embeddings
            logger.warning(f"[KG] Entity embedding failed for meeting '{request.meeting_id}': {exc}")

        logger.info(
            f"[KG] Meeting '{request.meeting_id}': {len(entity_list)} entities, {len(relations)} relations "
            f"({llm_ok}/{len(windows)} extraction calls usable)."
        )
        return KGExtractResponse(
            meeting_id=request.meeting_id, entities=entity_list, relations=list(relations.values()),
            llm_windows=attempted, llm_segments=llm_ok,
        )

    async def _extract_window(self, window: list[KGTurn], title: str | None) -> dict[str, Any] | None:
        lines = "\n".join(f"T{i + 1} {t.speaker}: {t.text.strip()}" for i, t in enumerate(window))
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Meeting: {title or 'Untitled'}\n\nTranscript excerpt:\n{lines}"},
        ]
        try:
            raw = await get_llm_client().generate_chat_completion(messages=messages, max_tokens=4096, temperature=0.0)
        except Exception as exc:
            logger.warning(f"[KG] Extraction call failed: {exc}")
            return None
        parsed = _parse_json_object(raw)
        if parsed is None:
            logger.warning("[KG] Extraction reply was not valid JSON; skipping this window.")
        return parsed

    @staticmethod
    def _merge_window(window, parsed, speaker_keys, add_entity, add_relation) -> None:
        """Adds one window's LLM entities/relations, mapping T-numbers to turn ids and names to entity keys."""

        def turn_ids(refs: Any) -> list[str]:
            out = []
            for ref in refs if isinstance(refs, list) else []:
                m = re.search(r"\d+", str(ref))
                if m and 1 <= int(m.group()) <= len(window):
                    out.append(window[int(m.group()) - 1].turn_id)
            return out

        # A first name the LLM used ("Priya") resolves to the speaker it belongs to ("Priya Sharma")
        first_names: dict[str, str] = {}
        for speaker, key in speaker_keys.items():
            first = normalize_name(speaker).split(" ")[0]
            first_names[first] = key if first not in first_names else ""  # ambiguous first names are not mapped

        name_to_key: dict[str, str] = {}
        for ent in parsed.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            name, etype = str(ent.get("name") or "").strip(), str(ent.get("type") or "").strip().lower().replace(" ", "_")
            grounded = turn_ids(ent.get("turns"))
            if not name or not grounded or etype not in ENTITY_TYPES or etype == "meeting":
                continue
            norm = normalize_name(name)
            if etype == "person":
                key = next((k for s, k in speaker_keys.items() if normalize_name(s) == norm), None) or first_names.get(norm)
                if key:  # merge into the speaker's entity (its key holds the normalized name)
                    add_entity("person", key.split(":", 1)[1], ent.get("description"), grounded)
                else:
                    key = add_entity("person", name, ent.get("description"), grounded)
            else:
                key = add_entity(etype, name, ent.get("description"), grounded)
            if key:
                name_to_key[norm] = key

        for rel in parsed.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            src = name_to_key.get(normalize_name(str(rel.get("source") or "")))
            dst = name_to_key.get(normalize_name(str(rel.get("target") or "")))
            rtype = str(rel.get("type") or "").strip().lower().replace(" ", "_")
            grounded = turn_ids(rel.get("turns"))
            if src and dst and grounded:
                add_relation(src, dst, rtype if rtype in RELATION_TYPES else "related_to", rel.get("description"), grounded, 0.8)
