import asyncio
from typing import Any, Dict, List, Optional, Union
from fastapi import APIRouter, Depends, HTTPException, status
from ..models.schemas import (
    RawTranscriptPayload,
    NormalizationResult,
    FetchAndNormalizeRequest,
    HierarchicalPipelineResult,
    TagSignalsRequest,
    SegmentTopicsRequest,
    ExtractIntelligenceRequest,
    SpeakerTurn,
    TopicSegment,
    Level3Summary,
)
from pydantic import BaseModel, Field
from ..services.normalizer import SpeakerNormalizer
from ..services.fireflies_client import FirefliesClient, FirefliesAPIError
from ..services.chunker import SignalTagger
from ..services.segmenter import TopicSegmenter
from ..services.summarizer import MeetingSummarizer
from ..services.pipeline_orchestrator import PipelineOrchestrator, PipelineExecutionResult
from ..services.embedding_service import EmbeddingService
from ..core.config import get_settings, Settings

router = APIRouter(prefix="/transcript", tags=["transcript"])


# ==============================================================================
# Dependency Providers
# ==============================================================================

def get_embedding_service(settings: Settings = Depends(get_settings)) -> EmbeddingService:
    return EmbeddingService(config=settings)


def get_normalizer() -> SpeakerNormalizer:
    return SpeakerNormalizer()


def get_fireflies_client(settings: Settings = Depends(get_settings)) -> FirefliesClient:
    return FirefliesClient(settings=settings)


def get_signal_tagger() -> SignalTagger:
    return SignalTagger()


def get_topic_segmenter(settings: Settings = Depends(get_settings)) -> TopicSegmenter:
    return TopicSegmenter(settings=settings)


def get_meeting_summarizer(settings: Settings = Depends(get_settings)) -> MeetingSummarizer:
    return MeetingSummarizer(settings=settings)


def get_pipeline_orchestrator(settings: Settings = Depends(get_settings)) -> PipelineOrchestrator:
    return PipelineOrchestrator(config=settings)


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


@router.post(
    "/fetch-and-normalize",
    response_model=NormalizationResult,
    status_code=status.HTTP_200_OK,
    summary="Fetch transcript from Fireflies GraphQL API and normalize",
    description="Queries Fireflies API for raw transcript sentences, then processes speaker turn normalization and signal detection.",
)
def fetch_and_normalize(
    request: FetchAndNormalizeRequest,
    client: FirefliesClient = Depends(get_fireflies_client),
    normalizer: SpeakerNormalizer = Depends(get_normalizer),
    tagger: SignalTagger = Depends(get_signal_tagger),
) -> NormalizationResult:
    """Thin handler delegating retrieval to FirefliesClient and processing to SpeakerNormalizer."""
    try:
        raw_payload = client.fetch_raw_transcript(
            meeting_id=request.meeting_id,
            api_key_override=request.api_key,
        )
    except FirefliesAPIError as exc:
        raise HTTPException(
            status_code=exc.status_code or status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    result = normalizer.normalize(raw_payload)
    result.turns = tagger.tag_turns(result.turns)
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
            s = max(0, int(round(float(sec or 0.0))))
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
    texts: List[str]


class EmbedTextsResponse(BaseModel):
    embeddings: List[List[float]]
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
    response_model=List[SpeakerTurn],
    status_code=status.HTTP_200_OK,
    summary="Tag Level 1 turns with conversational signals",
    description="Processes SpeakerTurns and flags questions, commitments, and blockers.",
)
def tag_signals_endpoint(
    request: TagSignalsRequest,
    tagger: SignalTagger = Depends(get_signal_tagger),
) -> List[SpeakerTurn]:
    """Thin handler delegating signal tagging to SignalTagger service."""
    return tagger.tag_turns(request.turns)


@router.post(
    "/segment-topics",
    response_model=List[TopicSegment],
    status_code=status.HTTP_200_OK,
    summary="Group Level 1 turns into Level 2 Topic Segments",
    description="Executes hybrid boundary detection with similarity guardrails and tangent protection.",
)
async def segment_topics_endpoint(
    request: SegmentTopicsRequest,
    segmenter: TopicSegmenter = Depends(get_topic_segmenter),
) -> List[TopicSegment]:
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


@router.post(
    "/orchestrate-phase3",
    response_model=PipelineExecutionResult,
    status_code=status.HTTP_200_OK,
    summary="Execute Phase 3 token chunking, boundary detection, embeddings & persistence pipeline",
    description="Executes 400-token sub-splitting with overlap, cosine similarity boundary detection, chapter generation, and PGVector persistence.",
)
async def orchestrate_phase3_endpoint(
    payload: RawTranscriptPayload,
    normalizer: SpeakerNormalizer = Depends(get_normalizer),
    tagger: SignalTagger = Depends(get_signal_tagger),
    orchestrator: PipelineOrchestrator = Depends(get_pipeline_orchestrator),
) -> PipelineExecutionResult:
    """Thin handler delegating full Phase 3 pipeline execution to PipelineOrchestrator."""
    norm_result = normalizer.normalize(payload)
    tagged_turns = tagger.tag_turns(norm_result.turns)
    
    return await orchestrator.execute_pipeline(
        meeting_id=payload.meeting_id,
        turns=tagged_turns,
        title=payload.title,
    )



# ==============================================================================
# Phase 4: Grounded Chatbot Synthesis Endpoint (RAG & OpenRouter LLM)
# ==============================================================================
from pydantic import BaseModel, Field
from ..services.llm_client import OpenRouterClient, get_openrouter_client

chat_router = APIRouter(prefix="/chat", tags=["chat"])


class ChatCitation(BaseModel):
    speaker: str
    start_time: float
    end_time: float
    meeting_id: str
    exact_raw_text: str
    topic_context: Optional[str] = "Level 1 Turn"


class ChatSynthesizeRequest(BaseModel):
    query: str
    context_type: str = "MICRO"
    citations: List[ChatCitation] = Field(default_factory=list)
    macro_summary: Optional[str] = None
    model: Optional[str] = None


class ChatSynthesizeResponse(BaseModel):
    query: str
    answer: str
    model_used: str
    routing_mode: str
    success: bool = True


@chat_router.post(
    "/synthesize",
    response_model=ChatSynthesizeResponse,
    status_code=status.HTTP_200_OK,
    summary="Synthesize grounded conversational answer via OpenRouter",
    description="Accepts user query and verified citations/context, formats system prompts, and calls OpenRouter.",
)
async def chat_synthesize_endpoint(
    request: ChatSynthesizeRequest,
    llm: OpenRouterClient = Depends(get_openrouter_client),
) -> ChatSynthesizeResponse:
    prompt = request.query.strip()
    model = request.model or llm.default_model

    if request.context_type == "MACRO":
        system_prompt = (
            "You are an executive meeting intelligence analyst. "
            "Synthesize a professional, clear, concise status summary based ONLY on the provided meeting overview data. "
            "Do not make up facts outside the provided context."
        )
        macro_info = request.macro_summary or "No portfolio data provided."
        user_content = f"Portfolio Overview Data:\n{macro_info}\n\nUser Question: {prompt}"
    else:
        formatted_context = "\n".join([
            f"[{i+1}] Speaker: {c.speaker} | Meeting: {c.meeting_id} | Time: {c.start_time:.1f}s - {c.end_time:.1f}s\nQuote: \"{c.exact_raw_text}\""
            for i, c in enumerate(request.citations)
        ])
        system_prompt = (
            "You are an AI meeting intelligence assistant. Answer the user's question accurately, fluently, "
            "and concisely, strictly grounded in the provided meeting transcript excerpts. "
            "Explicitly attribute key viewpoints and decisions to the named speakers and cite their timestamps when relevant. "
            "If the provided excerpts do not contain enough information to answer the question, state so honestly."
        )
        user_content = f"Verified Meeting Excerpts:\n{formatted_context}\n\nUser Question: {prompt}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    answer = await llm.generate_chat_completion(
        messages=messages,
        model=model,
        max_tokens=800,
        temperature=0.2,
    )

    return ChatSynthesizeResponse(
        query=prompt,
        answer=answer,
        model_used=model,
        routing_mode=request.context_type,
        success=True,
    )
