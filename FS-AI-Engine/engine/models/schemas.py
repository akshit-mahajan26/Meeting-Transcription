import re
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator


# ==============================================================================
# Raw Transcript Inputs (Fireflies schema mapping)
# ==============================================================================

class RawSentence(BaseModel):
    """Raw sentence representation as returned by Fireflies GraphQL API."""
    index: int = Field(..., description="Sentence order index from Fireflies")
    speaker_name: str = Field(..., description="Name of the speaker")
    speaker_id: Optional[str] = Field(default=None, description="Optional speaker identifier")
    text: str = Field(..., description="Transcribed sentence text")
    raw_text: Optional[str] = Field(default=None, description="Raw transcription before formatting")
    start_time: float = Field(..., ge=0.0, description="Start timestamp in seconds")
    end_time: float = Field(..., ge=0.0, description="End timestamp in seconds")


class RawTranscriptPayload(BaseModel):
    """Input payload containing meeting metadata and raw sentences to normalize."""
    meeting_id: str = Field(..., min_length=1, description="Unique identifier for the meeting")
    title: Optional[str] = Field(default=None, description="Meeting title or subject")
    date: Optional[str] = Field(default=None, description="ISO date string or timestamp of the meeting")
    duration: Optional[float] = Field(default=None, ge=0.0, description="Duration in seconds")
    sentences: List[RawSentence] = Field(default_factory=list, description="List of raw transcript sentences")


# ==============================================================================
# Level 1: Speaker Turns (Enhanced for Phase 2 with Signal Flags)
# ==============================================================================

class SpeakerTurn(BaseModel):
    """
    Level 1 Normalized Speaker Turn.
    Consecutive utterances from the same speaker are merged into a single turn.
    Strictly follows deterministic ID convention: mtg_<meeting_id>_turn_<NNN>
    Enhanced in Phase 2 with signal flags (questions, commitments, blockers).
    """
    turn_id: str = Field(
        ...,
        description="Deterministic unique turn ID following mtg_<id>_turn_<NNN>",
        examples=["mtg_m101_turn_001"],
    )
    meeting_id: str = Field(..., description="Parent meeting identifier")
    turn_number: int = Field(..., ge=1, description="Sequential 1-based turn number")
    speaker_id: str = Field(..., description="Normalized speaker ID")
    speaker_name: str = Field(..., description="Normalized speaker name")
    text: str = Field(..., description="Consolidated spoken text for this turn")
    start_time: float = Field(..., ge=0.0, description="Turn start time in seconds")
    end_time: float = Field(..., ge=0.0, description="Turn end time in seconds")
    duration: float = Field(..., ge=0.0, description="Turn duration in seconds")
    sentence_indices: List[int] = Field(
        default_factory=list,
        description="Source sentence indices merged into this turn",
    )

    # Phase 2: Level 1 Signal Detection Flags
    is_question: bool = Field(default=False, description="Flagged if turn contains interrogatives or clarification questions")
    is_commitment: bool = Field(default=False, description="Flagged if turn contains action commitments or promises")
    is_blocker: bool = Field(default=False, description="Flagged if turn articulates a dependency, roadblock, or impediment")
    signals: List[str] = Field(default_factory=list, description="Detailed list of detected signal tags e.g. ['question', 'commitment']")
    embedding: Optional[List[float]] = Field(default=None, description="1536-dimensional vector embedding")

    @field_validator("turn_id")
    @classmethod
    def validate_turn_id_format(cls, v: str) -> str:
        pattern = r"^mtg_[a-zA-Z0-9_\-]+_turn_\d{3,}$"
        if not re.match(pattern, v):
            raise ValueError(
                f"Invalid turn_id format '{v}'. Must match pattern mtg_<id>_turn_<NNN> (e.g. mtg_123_turn_001)"
            )
        return v


class NormalizationResult(BaseModel):
    """Response returned after running speaker turn normalization."""
    meeting_id: str
    title: Optional[str] = None
    date: Optional[str] = None
    duration: Optional[float] = None
    total_raw_sentences: int
    total_normalized_turns: int
    turns: List[SpeakerTurn]


# ==============================================================================
# Level 2: Topic Segments (Phase 2 Hybrid Boundary Model)
# ==============================================================================

class TopicSegment(BaseModel):
    """
    Level 2 Semantic Segment grouping multiple contiguous Level 1 turns by topic.
    Strictly follows deterministic ID convention: mtg_<id>_seg_<NN>
    """
    segment_id: str = Field(
        ...,
        description="Deterministic unique segment ID following mtg_<id>_seg_<NN>",
        examples=["mtg_m101_seg_01"],
    )
    meeting_id: str = Field(..., description="Parent meeting identifier")
    segment_index: int = Field(..., ge=1, description="Sequential 1-based segment index")
    title: str = Field(..., description="Concise headline or agenda topic for this segment")
    topic_summary: str = Field(..., description="Synthesized topic summary for this window")
    turn_ids: List[str] = Field(default_factory=list, description="Contiguous Level 1 turn IDs included in this segment")
    start_time: float = Field(..., ge=0.0, description="Segment start timestamp in seconds")
    end_time: float = Field(..., ge=0.0, description="Segment end timestamp in seconds")
    duration: float = Field(..., ge=0.0, description="Segment duration in seconds")
    turns_count: int = Field(..., ge=1, description="Number of turns in this segment")
    key_speakers: List[str] = Field(default_factory=list, description="Primary participants in this segment")
    boundary_confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence score of topic shift boundary")

    @field_validator("segment_id")
    @classmethod
    def validate_segment_id_format(cls, v: str) -> str:
        pattern = r"^mtg_[a-zA-Z0-9_\-]+_seg_\d{2,}$"
        if not re.match(pattern, v):
            raise ValueError(
                f"Invalid segment_id format '{v}'. Must match pattern mtg_<id>_seg_<NN> (e.g. mtg_123_seg_01)"
            )
        return v


# Backward compatibility alias
Level2Segment = TopicSegment


# ==============================================================================
# Level 3: Meeting Summary & Grounded Intelligence (Phase 2 Model)
# ==============================================================================

class GroundedItem(BaseModel):
    """
    Structured intelligence item (decision, commitment, or blocker)
    strictly grounded to immutable Level 1 source turn IDs.
    """
    text: str = Field(..., description="Extracted claim, decision, action, or blocker")
    source_chunk_ids: List[str] = Field(
        ...,
        min_length=1,
        description="List of Level 1 turn IDs (mtg_<id>_turn_<NNN>) providing empirical grounding",
    )
    owner: Optional[str] = Field(default=None, description="Responsible party or assignee if applicable")
    category: Optional[str] = Field(default=None, description="Optional classification tag")

    @field_validator("source_chunk_ids")
    @classmethod
    def validate_source_chunk_ids(cls, v: List[str]) -> List[str]:
        pattern = r"^mtg_[a-zA-Z0-9_\-]+_turn_\d{3,}$"
        for chunk_id in v:
            if not re.match(pattern, chunk_id):
                raise ValueError(
                    f"Invalid source_chunk_id '{chunk_id}'. Must reference Level 1 turn ID pattern mtg_<id>_turn_<NNN>"
                )
        return v


class Level3Summary(BaseModel):
    """
    Level 3 Executive Meeting Intelligence Node.
    Strictly follows deterministic ID convention: mtg_<id>_sum
    Contains verified decisions, commitments, and blockers grounded to Level 1 turn IDs.
    """
    summary_id: str = Field(
        ...,
        description="Deterministic summary ID following mtg_<id>_sum",
        examples=["mtg_m101_sum"],
    )
    meeting_id: str = Field(..., description="Parent meeting identifier")
    title: Optional[str] = Field(default=None, description="Meeting title")
    overview: str = Field(..., description="High-level executive overview of the entire meeting")
    decisions: List[GroundedItem] = Field(default_factory=list, description="Key decisions ratified with Level 1 grounding")
    commitments: List[GroundedItem] = Field(default_factory=list, description="Actionable commitments with Level 1 grounding")
    blockers: List[GroundedItem] = Field(default_factory=list, description="Identified blockers and dependencies with Level 1 grounding")
    participants: List[str] = Field(default_factory=list, description="Verified meeting participants")
    total_turns_analyzed: int = Field(default=0, ge=0)
    total_segments_analyzed: int = Field(default=0, ge=0)

    @field_validator("summary_id")
    @classmethod
    def validate_summary_id_format(cls, v: str) -> str:
        pattern = r"^mtg_[a-zA-Z0-9_\-]+_sum$"
        if not re.match(pattern, v):
            raise ValueError(
                f"Invalid summary_id format '{v}'. Must match pattern mtg_<id>_sum (e.g. mtg_123_sum)"
            )
        return v


# ==============================================================================
# Pipeline Request & Result Schemas
# ==============================================================================

class HierarchicalPipelineResult(BaseModel):
    """Unified response containing the full 3-level hierarchical intelligence tree."""
    meeting_id: str
    title: Optional[str] = None
    level1_turns: List[SpeakerTurn]
    level2_segments: List[TopicSegment]
    level3_summary: Level3Summary
    stats: Dict[str, Any] = Field(default_factory=dict)


class TagSignalsRequest(BaseModel):
    """Request to run signal detection on existing Level 1 turns."""
    turns: List[SpeakerTurn]


class SegmentTopicsRequest(BaseModel):
    """Request to run topic segmentation on Level 1 turns."""
    meeting_id: str
    turns: List[SpeakerTurn]
    similarity_threshold: Optional[float] = None
    min_turns: Optional[int] = None
    max_turns: Optional[int] = None


class ExtractIntelligenceRequest(BaseModel):
    """Request to generate Level 3 summary node from turns and segments."""
    meeting_id: str
    title: Optional[str] = None
    turns: List[SpeakerTurn]
    segments: Optional[List[TopicSegment]] = None


class FetchAndNormalizeRequest(BaseModel):
    """Request to fetch transcript from Fireflies GraphQL API and normalize it."""
    meeting_id: str = Field(..., description="Fireflies meeting/transcript ID")
    api_key: Optional[str] = Field(
        default=None,
        description="Optional API key override (falls back to FIREFLIES_API_KEY from env)",
    )
