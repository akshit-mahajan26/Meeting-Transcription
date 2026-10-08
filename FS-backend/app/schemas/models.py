import re
from datetime import datetime
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, Field, field_validator


# ==============================================================================
# Webhook Schemas
# ==============================================================================

class FirefliesWebhookPayload(BaseModel):
    """
    Standard webhook payload dispatched by Fireflies.ai when transcription completes.
    Supports both snake_case and camelCase field variations.
    """
    meeting_id: Optional[str] = Field(default=None, alias="meetingId", description="Meeting / Transcript ID")
    event_type: Optional[str] = Field(
        default="Transcription completed",
        alias="eventType",
        description="Event name, typically 'Transcription completed'",
    )
    client_reference_id: Optional[str] = Field(default=None, alias="clientReferenceId")
    data: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Metadata or transcript summary")

    @property
    def resolved_meeting_id(self) -> str:
        """Resolve meeting ID regardless of field naming convention."""
        mid = self.meeting_id or (self.data.get("meetingId") if self.data else None) or (self.data.get("id") if self.data else None)
        if not mid:
            raise ValueError("Meeting ID could not be found in webhook payload")
        return str(mid)

    model_config = {"populate_by_name": True}


class WebhookResponse(BaseModel):
    """Response returned upon acknowledging and processing a webhook event."""
    status: str
    message: str
    meeting_id: str
    total_turns: int = 0


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
    sentence_indices: List[int] = Field(default_factory=list)
    is_question: bool = Field(default=False)
    is_commitment: bool = Field(default=False)
    is_blocker: bool = Field(default=False)
    signals: List[str] = Field(default_factory=list)
    embedding: Optional[List[float]] = None
    metadata: Optional[Dict[str, Any]] = None

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

class RawSentenceInput(BaseModel):
    """Raw sentence representation for direct ingestion."""
    index: int
    speaker_name: str
    speaker_id: Optional[str] = None
    text: str
    raw_text: Optional[str] = None
    start_time: float
    end_time: float


class RawTranscriptIngestRequest(BaseModel):
    """Direct ingestion payload with raw sentences or Fireflies transcript payload."""
    meeting_id: str = Field(..., min_length=1, description="Unique identifier for the meeting")
    title: Optional[str] = Field(default=None, description="Title of the meeting")
    date: Optional[str] = Field(default=None, description="Date/timestamp string")
    duration: Optional[float] = Field(default=None, description="Meeting duration in seconds")
    sentences: List[RawSentenceInput] = Field(default_factory=list, description="Raw transcript sentences")


class FetchMeetingIngestRequest(BaseModel):
    """Trigger ingestion by fetching from Fireflies API via meeting ID."""
    meeting_id: str = Field(..., min_length=1, description="Fireflies meeting / transcript ID")
    api_key: Optional[str] = Field(default=None, description="Optional Fireflies API key override")


class MeetingResponse(BaseModel):
    """Complete meeting entity details with normalized speaker turns."""
    id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    date: Optional[str] = None
    duration: Optional[float] = None
    status: str
    project_id: Optional[str] = None
    total_turns: int
    created_at: datetime
    updated_at: datetime
    turns: Optional[List[SpeakerTurnResponse]] = None

    model_config = {"from_attributes": True}


class MeetingSummary(BaseModel):
    """Summary representation for meeting listing."""
    id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    date: Optional[str] = None
    duration: Optional[float] = None
    status: str
    project_id: Optional[str] = None
    total_turns: int
    created_at: datetime

    model_config = {"from_attributes": True}


class MeetingListResponse(BaseModel):
    """Paginated or listed meeting overview."""
    total: int
    meetings: List[MeetingSummary]


class ProjectListResponse(BaseModel):
    """List of all known project identifiers."""
    total: int
    projects: List[str]
