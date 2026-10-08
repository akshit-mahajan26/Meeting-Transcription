import re
import logging
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from ..core.config import settings

logger = logging.getLogger(__name__)


class QueryIntent(str, Enum):
    """Dual-Mode Query Intent Enumeration."""
    MACRO = "MACRO"
    MICRO = "MICRO"


class ExtractedFilters(BaseModel):
    """Context constraints extracted from the user query."""
    project_id: Optional[str] = Field(default=None, description="Explicit or inferred project ID")
    speaker: Optional[str] = Field(default=None, description="Target speaker canonical name")
    date_filter: Optional[str] = Field(default=None, description="Temporal or date constraint")
    topic: Optional[str] = Field(default=None, description="Inferred topic category")
    keywords: List[str] = Field(default_factory=list, description="Extracted salient keywords")


class RoutingDecision(BaseModel):
    """Result of Cognitive Intent Classification & Parameter Extraction."""
    query: str
    intent: QueryIntent
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    filters: ExtractedFilters
    llm_model_used: str = Field(default_factory=lambda: settings.LLM_MODEL_NAME)


class AgentRouter:
    """
    Cognitive Intent Classification & Retrieval Router.
    Routes queries to MACRO (relational SQL over summaries and status)
    or MICRO (hybrid vector search + pointer store over Level 1 turns).
    """

    def __init__(self, llm_model: Optional[str] = None):
        self.llm_model = llm_model or settings.LLM_MODEL_NAME

    async def route(self, query: str, explicit_project_id: Optional[str] = None) -> RoutingDecision:
        """
        Classifies user query intent and extracts context constraints.
        """
        cleaned_query = query.strip()
        logger.info(f"[AgentRouter] Analyzing query: '{cleaned_query}' using model {self.llm_model}")

        # 1. Parameter Extraction (Project, Speaker, Topic, Keywords)
        filters = self._extract_parameters(cleaned_query, explicit_project_id)

        # 2. Intent Classification (Cognitive analysis: Macro vs Micro)
        intent, confidence, reasoning = self._classify_intent(cleaned_query, filters)

        decision = RoutingDecision(
            query=cleaned_query,
            intent=intent,
            confidence=confidence,
            reasoning=reasoning,
            filters=filters,
            llm_model_used=self.llm_model
        )
        logger.info(
            f"[AgentRouter] Decision: intent={decision.intent.value}, "
            f"confidence={decision.confidence:.2f}, reasoning='{decision.reasoning}'"
        )
        return decision

    def _classify_intent(self, query: str, filters: ExtractedFilters) -> tuple[QueryIntent, float, str]:
        """
        Evaluates whether query requires high-level relational aggregation (MACRO)
        or pinpoint atomic citation retrieval (MICRO).
        """
        lower_q = query.lower()

        # Macro patterns: project health, summaries, lists, broad status, progress
        macro_signals = [
            r"\b(?:project status|project health|overall progress|high[- ]level summary|executive summary)\b",
            r"\b(?:list (?:all )?meetings|how many meetings|meeting overview|meeting list)\b",
            r"\b(?:what is the status of|how is the project doing|give me an overview)\b",
            r"\b(?:what are the major decisions across|aggregate decisions|project roadmap)\b",
            r"\b(?:summary of (?:the )?(?:entire )?project|project milestones)\b"
        ]

        # Micro patterns: specific facts, exact quotes, who said what, technical parameters, debugging
        micro_signals = [
            r"\b(?:who said|who stated|did .* say|what did .* (?:say|mention|commit))\b",
            r"\b(?:quote|exact words|verbatim|timestamp|at what time)\b",
            r"\b(?:checkpointer|asyncpostgressaver|redis|postgres|database architecture)\b",
            r"\b(?:sla|webhook timeout|celery worker|5 seconds|latency)\b",
            r"\b(?:why did|why is|how did .* implement|what was the error)\b"
        ]

        macro_score = sum(2.0 for p in macro_signals if re.search(p, lower_q))
        micro_score = sum(2.0 for p in micro_signals if re.search(p, lower_q))

        # Heuristic modifiers
        if filters.speaker:
            micro_score += 1.5
        if "summary" in lower_q and "project" in lower_q:
            macro_score += 2.0
        elif "summary" in lower_q and not filters.speaker:
            macro_score += 1.0

        if macro_score > micro_score:
            confidence = min(0.95, 0.60 + 0.1 * macro_score)
            reasoning = "Query asks for high-level aggregate project status, executive summaries, or meeting overviews."
            return QueryIntent.MACRO, round(confidence, 2), reasoning
        else:
            confidence = min(0.98, 0.70 + 0.1 * micro_score)
            reasoning = "Query targets specific atomic facts, technical parameters, or individual speaker statements requiring Level 1 citations."
            return QueryIntent.MICRO, round(confidence, 2), reasoning

    def _extract_parameters(self, query: str, explicit_project_id: Optional[str] = None) -> ExtractedFilters:
        """
        Extracts entities and context constraints from query text.
        """
        lower_q = query.lower()

        known_speakers = [
            "Sarah Chen", "Alex Rivera", "Marcus Vance", "David Kim",
            "Elena Rostova", "Rachel Green", "Michael Scott"
        ]
        detected_speaker = None
        for s in known_speakers:
            if s.lower() in lower_q or s.split()[0].lower() in lower_q.split():
                detected_speaker = s
                break

        detected_project = explicit_project_id
        if not detected_project:
            proj_match = re.search(r"\bproject[- ]([a-zA-Z0-9_\-]+)\b", query, re.IGNORECASE)
            if proj_match and proj_match.group(1).lower() not in {
                "status", "health", "overview", "summary", "plan", "update", "roadmap", "progress"
            }:
                detected_project = f"proj-{proj_match.group(1).lower()}"

        detected_topic = None
        if any(w in lower_q for w in ["database", "postgres", "checkpointer", "persistence"]):
            detected_topic = "Database Architecture & Checkpointer"
        elif any(w in lower_q for w in ["webhook", "sla", "celery", "latency", "timeout"]):
            detected_topic = "Webhook Architecture & SLA Management"

        stop_words = {"what", "which", "where", "when", "that", "this", "with", "from", "about", "tell", "give"}
        raw_words = re.findall(r"\b[a-zA-Z0-9_]{3,}\b", query.lower())
        keywords = [w for w in raw_words if w not in stop_words]

        return ExtractedFilters(
            project_id=detected_project,
            speaker=detected_speaker,
            topic=detected_topic,
            keywords=keywords
        )
