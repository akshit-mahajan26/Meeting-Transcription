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

    Extraction is deterministic (pattern matching on tagged turns); no LLM call is made.
    """

    DECISION_PATTERNS: ClassVar[list[re.Pattern]] = [
        re.compile(r"\b(decided to|we decided|decision is|agreed to|agreed that|approved)\b", re.IGNORECASE),
        re.compile(r"\b(we will go with|the consensus is|settled on|final choice)\b", re.IGNORECASE),
        re.compile(r"\b(let's proceed with|moving forward with|conclusion is)\b", re.IGNORECASE),
    ]

    # Transcript characters sent when writing the overview (long meetings are sampled evenly, not cut off)
    OVERVIEW_PROMPT_CHARS: ClassVar[int] = 60_000

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def generate_intelligence(
        self,
        meeting_id: str,
        turns: list[SpeakerTurn],
        segments: list[TopicSegment] | None = None,
        title: str | None = None,
        write_overview: bool = False,
    ) -> Level3Summary:
        """
        Generate Level 3 meeting intelligence node strictly grounded to Level 1 source chunk IDs.

        Args:
            meeting_id: Meeting identifier.
            turns: List of tagged Level 1 SpeakerTurn objects.
            segments: Optional list of Level 2 TopicSegment objects.
            title: Optional meeting title.
            write_overview: Ask the LLM for a short written overview (only worth it when the meeting has no
                Fireflies summary). Falls back to the template overview on any failure.

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

        result = self._extract_deterministically(summary_id, meeting_id, turns, segments, title)
        if write_overview and self.settings.GEMINI_API_KEY:
            try:
                written = self._write_overview(turns, segments, title)
                if written:
                    result.overview = written
            except Exception as exc:
                logger.warning(f"LLM overview failed ({exc}). Keeping the template overview.")
        return result

    def _write_overview(
        self, turns: list[SpeakerTurn], segments: list[TopicSegment] | None, title: str | None
    ) -> str | None:
        """One short LLM call: a 3-5 sentence overview of what was discussed. Returns None if the call fails."""
        lines = [f"{t.speaker_name}: {t.text}" for t in turns]
        total = sum(len(line) + 1 for line in lines)
        if total > self.OVERVIEW_PROMPT_CHARS:
            keep_every = -(-total // self.OVERVIEW_PROMPT_CHARS)  # ceil: spread the sample across the whole meeting
            lines = [line[:600] for line in lines[::keep_every]]
        topics = "; ".join(seg.title for seg in segments) if segments else "not available"
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You write concise, factual meeting overviews. Use only what the transcript says; never invent details.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Meeting title: {title or 'Untitled'}\nTopics: {topics}\n\nTranscript:\n" + "\n".join(lines)
                        + "\n\nWrite a 3-5 sentence overview of what was discussed and any outcomes. Plain text, no headings or bullets."
                    ),
                },
            ],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.settings.GEMINI_API_KEY}", "Content-Type": "application/json"}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{self.settings.GEMINI_BASE_URL.rstrip('/')}/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            logger.error(f"LLM overview returned status {resp.status_code}")
            return None
        text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        return text or None

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
