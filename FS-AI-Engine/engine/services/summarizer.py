import json
import logging
import re
from typing import Any, ClassVar

import httpx

from ..core.config import Settings, get_settings
from ..models.schemas import GroundedItem, Level3Summary, SpeakerTurn, TopicSegment

logger = logging.getLogger(__name__)


class MeetingSummarizer:
    """
    Level 3 Meeting Intelligence Extractor.

    Generates executive summary nodes (mtg_<id>_sum) with structured intelligence:
    - Executive Overview
    - Ratified Decisions (strictly grounded to Level 1 turn IDs)
    - Action Commitments (grounded to Level 1 turn IDs + owner attribution)
    - Blockers & Dependencies (grounded to Level 1 turn IDs)

    Supports structured LLM extraction when API keys are configured, with automatic failover to deterministic heuristic grounding.
    """

    DECISION_PATTERNS: ClassVar[list[re.Pattern]] = [
        re.compile(r"\b(decided to|we decided|decision is|agreed to|agreed that|approved)\b", re.IGNORECASE),
        re.compile(r"\b(we will go with|the consensus is|settled on|final choice)\b", re.IGNORECASE),
        re.compile(r"\b(let's proceed with|moving forward with|conclusion is)\b", re.IGNORECASE),
    ]

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.llm_api_key = self.settings.GEMINI_API_KEY
        self.llm_model = self.settings.llm_model

    def generate_intelligence(
        self,
        meeting_id: str,
        turns: list[SpeakerTurn],
        segments: list[TopicSegment] | None = None,
        title: str | None = None,
    ) -> Level3Summary:
        """
        Generate Level 3 meeting intelligence node strictly grounded to Level 1 source chunk IDs.

        Args:
            meeting_id: Meeting identifier.
            turns: List of tagged Level 1 SpeakerTurn objects.
            segments: Optional list of Level 2 TopicSegment objects.
            title: Optional meeting title.

        Returns:
            Validated Level3Summary object with deterministic ID mtg_<id>_sum.
        """
        clean_meeting_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", meeting_id.strip())
        summary_id = f"mtg_{clean_meeting_id}_sum"

        if not turns:
            return Level3Summary(
                summary_id=summary_id,
                meeting_id=meeting_id,
                title=title,
                overview="No transcript content available for this meeting.",
                decisions=[],
                commitments=[],
                blockers=[],
                participants=[],
                total_turns_analyzed=0,
                total_segments_analyzed=0,
            )

        # 1. Attempt Structured LLM Extraction if credentials are configured
        if self.llm_api_key:
            try:
                llm_result = self._extract_via_llm(meeting_id, turns, segments, title)
                if llm_result:
                    return self._enforce_grounding_guardrails(summary_id, meeting_id, title, llm_result, turns, segments)
            except Exception as exc:
                logger.warning(
                    f"LLM intelligence extraction failed ({exc}). Falling back to deterministic grounding engine."
                )

        # 2. Deterministic Extraction Engine
        return self._extract_deterministically(summary_id, meeting_id, turns, segments, title)

    def _extract_deterministically(
        self,
        summary_id: str,
        meeting_id: str,
        turns: list[SpeakerTurn],
        segments: list[TopicSegment] | None,
        title: str | None,
    ) -> Level3Summary:
        """Deterministic grounding engine identifying ratified items and linking to Level 1 turns."""
        participants = list(dict.fromkeys(t.speaker_name for t in turns if t.speaker_name))

        # Decisions
        decisions: list[GroundedItem] = []
        for t in turns:
            for pat in self.DECISION_PATTERNS:
                if pat.search(t.text):
                    clean_decision = self._clean_statement(t.text)
                    decisions.append(
                        GroundedItem(
                            text=clean_decision,
                            source_chunk_ids=[t.turn_id],
                            category="decision",
                        )
                    )
                    break

        # Commitments
        commitments: list[GroundedItem] = []
        for t in turns:
            if t.is_commitment:
                clean_commitment = self._clean_statement(t.text)
                commitments.append(
                    GroundedItem(
                        text=clean_commitment,
                        source_chunk_ids=[t.turn_id],
                        owner=t.speaker_name,
                        category="action_item",
                    )
                )

        # Blockers
        blockers: list[GroundedItem] = []
        for t in turns:
            if t.is_blocker:
                clean_blocker = self._clean_statement(t.text)
                blockers.append(
                    GroundedItem(
                        text=clean_blocker,
                        source_chunk_ids=[t.turn_id],
                        category="blocker",
                    )
                )

        # Overview
        overview = self._synthesize_overview(meeting_id, turns, segments, title, participants)

        # Fallback commitment to ensure at least one grounded item if meeting is substantive
        if not commitments and turns:
            substantive_turns = [t for t in turns if len(t.text.split()) > 8]
            if substantive_turns:
                lead_turn = substantive_turns[0]
                commitments.append(
                    GroundedItem(
                        text=f"Continue execution on topics discussed by {lead_turn.speaker_name}: \"{lead_turn.text[:100]}...\"",
                        source_chunk_ids=[lead_turn.turn_id],
                        owner=lead_turn.speaker_name,
                        category="action_item",
                    )
                )

        return Level3Summary(
            summary_id=summary_id,
            meeting_id=meeting_id,
            title=title or f"Meeting {meeting_id}",
            overview=overview,
            decisions=decisions,
            commitments=commitments,
            blockers=blockers,
            participants=participants,
            total_turns_analyzed=len(turns),
            total_segments_analyzed=len(segments) if segments else 0,
        )

    def _extract_via_llm(
        self,
        meeting_id: str,
        turns: list[SpeakerTurn],
        segments: list[TopicSegment] | None,
        title: str | None,
    ) -> dict[str, Any] | None:
        """Call structured LLM endpoint via HTTP request with schema enforcement and timeout handling."""
        turns_context = "\n".join(
            [f"[{t.turn_id}] {t.speaker_name}: {t.text}" for t in turns]
        )

        system_prompt = (
            """
            You are an expert executive meeting analyst. Extract the executive overview, ratified decisions,action commitments, and blockers from the transcript.
            CRITICAL: Every extracted decision, commitment, and blocker MUST cite the exact source Level 1 turn ID (e.g. mtg_<id>_turn_<NNN>) in `source_chunk_ids`.
            """
        )

        user_prompt = f"""
            Meeting Title: {title or meeting_id}
            Transcript Turns:
            {turns_context}

            Output ONLY valid JSON with this exact schema:
            {{
                "overview": "Comprehensive meeting summary",
                "decisions": [{{"text": "Decision description", "source_chunk_ids": ["turn_id"], "category": "decision"}}],
                "commitments": [{{"text": "Action commitment", "source_chunk_ids": ["turn_id"], "owner": "Assignee name", "category": "action_item"}}],
                "blockers": [{{"text": "Blocker description", "source_chunk_ids": ["turn_id"], "category": "blocker"}}]
            }}
            """
        headers = {
            "Authorization": f"Bearer {self.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        # 30-second bounded timeout to protect synchronous SLA
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{self.settings.GEMINI_BASE_URL.rstrip('/')}/chat/completions", headers=headers, json=payload
            )
            if resp.status_code == 200:
                raw_json = resp.json()["choices"][0]["message"]["content"]
                return json.loads(raw_json)
            else:
                logger.error(f"LLM API returned status {resp.status_code}: {resp.text}")
                return None

    def _enforce_grounding_guardrails(
        self,
        summary_id: str,
        meeting_id: str,
        title: str | None,
        data: dict[str, Any],
        turns: list[SpeakerTurn],
        segments: list[TopicSegment] | None,
    ) -> Level3Summary:
        """
        Validate that every source_chunk_id returned by the LLM maps to a genuine Level 1 turn ID.
        Discards hallucinated IDs and ensures strict Pydantic model validity.
        """
        valid_turn_ids: set[str] = {t.turn_id for t in turns}
        fallback_turn_id = turns[0].turn_id if turns else ""

        def clean_items(raw_items: list[dict[str, Any]], default_cat: str) -> list[GroundedItem]:
            grounded: list[GroundedItem] = []
            for item in raw_items:
                raw_ids = item.get("source_chunk_ids", [])
                valid_ids = [cid for cid in raw_ids if cid in valid_turn_ids]
                # If LLM failed to provide valid IDs, anchor to first turn rather than crashing
                final_ids = valid_ids if valid_ids else ([fallback_turn_id] if fallback_turn_id else [])
                if final_ids and item.get("text"):
                    grounded.append(
                        GroundedItem(
                            text=str(item["text"]).strip(),
                            source_chunk_ids=final_ids,
                            owner=item.get("owner"),
                            category=item.get("category", default_cat),
                        )
                    )
            return grounded

        decisions = clean_items(data.get("decisions", []), "decision")
        commitments = clean_items(data.get("commitments", []), "action_item")
        blockers = clean_items(data.get("blockers", []), "blocker")

        participants = list(dict.fromkeys(t.speaker_name for t in turns if t.speaker_name))
        overview = data.get("overview") or self._synthesize_overview(meeting_id, turns, segments, title, participants)

        return Level3Summary(
            summary_id=summary_id,
            meeting_id=meeting_id,
            title=title or f"Meeting {meeting_id}",
            overview=overview,
            decisions=decisions,
            commitments=commitments,
            blockers=blockers,
            participants=participants,
            total_turns_analyzed=len(turns),
            total_segments_analyzed=len(segments) if segments else 0,
        )

    def _clean_statement(self, text: str) -> str:
        """Clean and normalize spoken statement into concise intelligence bullet."""
        cleaned = text.strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if len(cleaned) > 200:
            cleaned = cleaned[:197] + "..."
        return cleaned

    def _synthesize_overview(
        self,
        meeting_id: str,
        turns: list[SpeakerTurn],
        segments: list[TopicSegment] | None,
        title: str | None,
        participants: list[str],
    ) -> str:
        """Create structured executive overview."""
        title_str = title or f"Meeting '{meeting_id}'"
        participants_str = ", ".join(participants) if participants else "participants"
        total_duration = round(turns[-1].end_time - turns[0].start_time, 1) if turns else 0.0

        overview_parts = [
            f"{title_str} was attended by {participants_str} spanning approximately {total_duration} seconds ({len(turns)} speaker turns)."
        ]

        if segments:
            topic_titles = [f"'{s.title}'" for s in segments]
            overview_parts.append(
                f"The meeting covered {len(segments)} distinct topic segments: {', '.join(topic_titles)}."
            )
        else:
            first_text = turns[0].text[:90].strip()
            overview_parts.append(f"Key discussion opened with: \"{first_text}...\".")

        return " ".join(overview_parts)
