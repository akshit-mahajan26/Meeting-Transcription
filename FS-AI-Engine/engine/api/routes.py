import asyncio

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from ..core.config import Settings, get_settings
from ..models.schemas import (
    ExtractIntelligenceRequest,
    HierarchicalPipelineResult,
    Level3Summary,
    NormalizationResult,
    RawTranscriptPayload,
    SegmentTopicsRequest,
    SpeakerTurn,
    TagSignalsRequest,
    TopicSegment,
)
from ..services.embedding_service import EmbeddingService
from ..services.normalizer import SpeakerNormalizer
from ..services.segmenter import TopicSegmenter
from ..services.signal_tagger import SignalTagger
from ..services.summarizer import MeetingSummarizer

router = APIRouter(prefix="/transcript", tags=["transcript"])


# ==============================================================================
# Dependency Providers
# ==============================================================================

def get_embedding_service(settings: Settings = Depends(get_settings)) -> EmbeddingService:
    return EmbeddingService(config=settings)


def get_normalizer() -> SpeakerNormalizer:
    return SpeakerNormalizer()




def get_signal_tagger() -> SignalTagger:
    return SignalTagger()


def get_topic_segmenter(settings: Settings = Depends(get_settings)) -> TopicSegmenter:
    return TopicSegmenter(settings=settings)


def get_meeting_summarizer(settings: Settings = Depends(get_settings)) -> MeetingSummarizer:
    return MeetingSummarizer(settings=settings)




# ==============================================================================
# Phase 1 Endpoints (Enhanced with signal detection)
# ==============================================================================

@router.post(
    "/normalize",
    response_model=NormalizationResult,
    status_code=status.HTTP_200_OK,
    summary="Normalize raw transcript sentences into Level 1 turns",
    description="Merges consecutive utterances by the same speaker, assigns deterministic turn IDs, and tags conversational signals.",
)
def normalize_transcript(
    payload: RawTranscriptPayload,
    normalizer: SpeakerNormalizer = Depends(get_normalizer),
    tagger: SignalTagger = Depends(get_signal_tagger),
) -> NormalizationResult:
    """Thin handler delegating transcript normalization to SpeakerNormalizer service."""
    result = normalizer.normalize(payload)
    # Tag Level 1 turns with signal flags
    tagged_turns = tagger.tag_turns(result.turns)
    result.turns = tagged_turns
    return result




# ==============================================================================
# Phase 2 Endpoints: Full Hierarchical Intelligence Pipeline
# ==============================================================================

@router.post(
    "/process-full",
    response_model=HierarchicalPipelineResult,
    status_code=status.HTTP_200_OK,
    summary="Execute full Phase 2 hierarchical intelligence pipeline",
    description="Executes Level 1 normalization & tagging, embedding generation, Level 2 topic segmentation, and Level 3 grounded intelligence synthesis.",
)
async def process_full_pipeline(
    payload: RawTranscriptPayload,
    normalizer: SpeakerNormalizer = Depends(get_normalizer),
    tagger: SignalTagger = Depends(get_signal_tagger),
    segmenter: TopicSegmenter = Depends(get_topic_segmenter),
    summarizer: MeetingSummarizer = Depends(get_meeting_summarizer),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
) -> HierarchicalPipelineResult:
    """
    Orchestrator route for Hierarchical Intelligence Pipeline:
    1. Normalizes raw sentences into Level 1 turns (mtg_<id>_turn_<NNN>).
    2. Tags Level 1 signals (questions, commitments, blockers).
    3. Generates 1536-dimensional vector embeddings for all Level 1 turns.
    4. Segments contiguous turns into Level 2 topic segments (mtg_<id>_seg_<NN>).
    5. Extracts Level 3 grounded intelligence summary (mtg_<id>_sum).
    """
    # 1. Level 1 Normalization
    norm_result = normalizer.normalize(payload)

    # 2. Level 1 Signal Tagging
    tagged_turns = tagger.tag_turns(norm_result.turns)

    # 3 & 4. Concurrent Level 1 Embedding Generation & Level 2 Topic Segmentation
    async def _embed_turns():
        if not tagged_turns:
            return
        def _fmt_ts(sec: float) -> str:
            s = max(0, round(float(sec or 0.0)))
            return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

        turn_texts = [
            f"[{_fmt_ts(t.start_time)}] {t.speaker_name}: {t.text}"
            for t in tagged_turns
        ]
        try:
            vectors = await embedding_service.generate_embeddings(turn_texts)
            for idx, vec in enumerate(vectors):
                if idx < len(tagged_turns):
                    tagged_turns[idx].embedding = vec
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(f"Failed to generate embeddings in process_full_pipeline: {exc}")

    _, segments = await asyncio.gather(
        _embed_turns(),
        segmenter.segment_transcript_async(
            meeting_id=payload.meeting_id,
            turns=tagged_turns,
        ),
    )

    # 5. Level 3 Meeting Intelligence Extraction
    summary = summarizer.generate_intelligence(
        meeting_id=payload.meeting_id,
        turns=tagged_turns,
        segments=segments,
        title=payload.title,
    )

    stats = {
        "raw_sentences_count": len(payload.sentences),
        "level1_turns_count": len(tagged_turns),
        "level2_segments_count": len(segments),
        "decisions_count": len(summary.decisions),
        "commitments_count": len(summary.commitments),
        "blockers_count": len(summary.blockers),
    }

    return HierarchicalPipelineResult(
        meeting_id=payload.meeting_id,
        title=payload.title,
        level1_turns=tagged_turns,
        level2_segments=segments,
        level3_summary=summary,
        stats=stats,
    )


class EmbedTextsRequest(BaseModel):
    texts: list[str]


class EmbedTextsResponse(BaseModel):
    embeddings: list[list[float]]
    dimensions: int


@router.post(
    "/embed",
    response_model=EmbedTextsResponse,
    status_code=status.HTTP_200_OK,
    summary="Batch generate vector embeddings",
    description="Generates 1536-dimensional vector embeddings for provided texts via Gemini.",
)
async def embed_texts_endpoint(
    request: EmbedTextsRequest,
    embedding_service: EmbeddingService = Depends(get_embedding_service),
) -> EmbedTextsResponse:
    vectors = await embedding_service.generate_embeddings(request.texts)
    dims = len(vectors[0]) if vectors else 1536
    return EmbedTextsResponse(embeddings=vectors, dimensions=dims)



@router.post(
    "/tag-signals",
    response_model=list[SpeakerTurn],
    status_code=status.HTTP_200_OK,
    summary="Tag Level 1 turns with conversational signals",
    description="Processes SpeakerTurns and flags questions, commitments, and blockers.",
)
def tag_signals_endpoint(
    request: TagSignalsRequest,
    tagger: SignalTagger = Depends(get_signal_tagger),
) -> list[SpeakerTurn]:
    """Thin handler delegating signal tagging to SignalTagger service."""
    return tagger.tag_turns(request.turns)


@router.post(
    "/segment-topics",
    response_model=list[TopicSegment],
    status_code=status.HTTP_200_OK,
    summary="Group Level 1 turns into Level 2 Topic Segments",
    description="Executes hybrid boundary detection with similarity guardrails and tangent protection.",
)
async def segment_topics_endpoint(
    request: SegmentTopicsRequest,
    segmenter: TopicSegmenter = Depends(get_topic_segmenter),
) -> list[TopicSegment]:
    """Thin handler delegating topic segmentation to TopicSegmenter service."""
    return await segmenter.segment_transcript_async(
        meeting_id=request.meeting_id,
        turns=request.turns,
        similarity_threshold_override=request.similarity_threshold,
        min_turns_override=request.min_turns,
        max_turns_override=request.max_turns,
    )


@router.post(
    "/extract-intelligence",
    response_model=Level3Summary,
    status_code=status.HTTP_200_OK,
    summary="Extract Level 3 meeting intelligence",
    description="Synthesizes executive overview, decisions, commitments, and blockers grounded to Level 1 turn IDs.",
)
def extract_intelligence_endpoint(
    request: ExtractIntelligenceRequest,
    summarizer: MeetingSummarizer = Depends(get_meeting_summarizer),
) -> Level3Summary:
    """Thin handler delegating intelligence extraction to MeetingSummarizer service."""
    return summarizer.generate_intelligence(
        meeting_id=request.meeting_id,
        turns=request.turns,
        segments=request.segments,
        title=request.title,
    )





# ==============================================================================
# Phase 4: Grounded Chatbot Synthesis Endpoint (RAG & OpenRouter LLM)
# ==============================================================================









