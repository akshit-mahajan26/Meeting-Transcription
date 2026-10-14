import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ==============================================================================
# Fireflies / Meeting Schemas
# ==============================================================================





# ==============================================================================
# Level 1 Speaker Turn Schemas
# ==============================================================================

class SpeakerTurnResponse(BaseModel):
    """Normalized Level 1 Speaker Turn representation."""
    turn_id: str = Field(..., description="Deterministic turn ID conforming to mtg_<id>_turn_<NNN>")
    meeting_id: str
    turn_number: int
    speaker_id: str
    speaker_name: str
    text: str
    start_time: float
    end_time: float
    duration: float
    sentence_indices: list[int] = Field(default_factory=list)
    is_question: bool = Field(default=False)
    is_commitment: bool = Field(default=False)
    is_blocker: bool = Field(default=False)
    signals: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("turn_id")
    @classmethod
    def validate_turn_id_format(cls, v: str) -> str:
        pattern = r"^mtg_[a-zA-Z0-9_\-]+_turn_\d{3,}$"
        if not re.match(pattern, v):
            raise ValueError(
                f"Invalid turn_id format '{v}'. Must match pattern mtg_<id>_turn_<NNN>"
            )
        return v

    model_config = {"from_attributes": True}


# ==============================================================================
# Ingestion & Meeting Schemas
# ==============================================================================







class MeetingResponse(BaseModel):
    """Complete meeting entity details with normalized speaker turns."""
    id: str
    title: str | None = None
    summary: str | None = None
    date: str | None = None
    duration: float | None = None
    status: str
    project_id: str | None = None
    total_turns: int
    created_at: datetime
    updated_at: datetime
    turns: list[SpeakerTurnResponse] | None = None

    model_config = {"from_attributes": True}


class MeetingSummary(BaseModel):
    """Summary representation for meeting listing."""
    id: str
    title: str | None = None
    summary: str | None = None
    date: str | None = None
    duration: float | None = None
    status: str
    project_id: str | None = None
    total_turns: int
    created_at: datetime

    model_config = {"from_attributes": True}


class MeetingListResponse(BaseModel):
    """Paginated or listed meeting overview."""
    total: int
    meetings: list[MeetingSummary]


class ProjectListResponse(BaseModel):
    """List of all known project identifiers."""
    total: int
    projects: list[str]
