import asyncio
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath("FS-AI-Engine"))

from engine.core.config import get_settings
from engine.main import app as engine_app
from engine.models.schemas import (
    RawSentence,
    RawTranscriptPayload,
    SpeakerTurn,
)
from engine.services.normalizer import SpeakerNormalizer
from engine.services.segmenter import TopicSegmenter
from engine.services.signal_tagger import SignalTagger
from engine.services.summarizer import MeetingSummarizer

# The engine only answers calls that carry the shared internal key (as FS-backend sends)
client = TestClient(engine_app, headers={"X-Internal-Api-Key": get_settings().engine_api_key})


# Sample meeting transcript containing questions, commitments, blockers, and topic transitions
SAMPLE_PAYLOAD = RawTranscriptPayload(
    meeting_id="m_arch_sync_42",
    title="Core Architecture & Scaling Sync",
    sentences=[
        # Topic 1: Database performance & blocking issue
        RawSentence(
            index=0,
            speaker_name="Sarah",
            text="Good morning everyone. What is causing the SQLite lock contention in staging?",
            start_time=0.0,
            end_time=3.5,
        ),
        RawSentence(
            index=1,
            speaker_name="Alex",
            text="We are currently blocked by the connection pool settings. It is a major blocker for our deployment.",
            start_time=4.0,
            end_time=8.5,
        ),
        RawSentence(
            index=2,
            speaker_name="Sarah",
            text="Understood. We decided to increase the timeout and configure WAL mode immediately.",
            start_time=9.0,
            end_time=13.0,
        ),
        RawSentence(
            index=3,
            speaker_name="Alex",
            text="I will update the database configuration by tomorrow morning.",
            start_time=13.5,
            end_time=17.0,
        ),
        # Topic 2: Frontend UI & Streamlit Migration
        RawSentence(
            index=4,
            speaker_name="Elena",
            text="Let's pivot to the user interface layer and discuss the Streamlit components.",
            start_time=18.0,
            end_time=22.0,
        ),
        RawSentence(
            index=5,
            speaker_name="Elena",
            text="We need to make sure the Level 1 turns and citation cards render smoothly in the browser.",
            start_time=22.5,
            end_time=27.0,
        ),
        RawSentence(
            index=6,
            speaker_name="Sarah",
            text="Can you review the transcript viewer component Elena?",
            start_time=27.5,
            end_time=30.0,
        ),
        RawSentence(
            index=7,
            speaker_name="Elena",
            text="Yes, I will take on the UI review and deliver the updated views by Friday.",
            start_time=30.5,
            end_time=35.0,
        ),
    ],
)


def test_level1_signal_tagger():
    tagger = SignalTagger()

    # Question detection
    q_turn = SpeakerTurn(
        turn_id="mtg_test_turn_001",
        meeting_id="test",
        turn_number=1,
        speaker_id="sarah",
        speaker_name="Sarah",
        text="What is causing the SQLite lock contention in staging?",
        start_time=0.0,
        end_time=3.0,
        duration=3.0,
    )
    tagged_q = tagger.tag_turn(q_turn)
    assert tagged_q.is_question is True
    assert "question" in tagged_q.signals

    # Blocker detection
    b_turn = SpeakerTurn(
        turn_id="mtg_test_turn_002",
        meeting_id="test",
        turn_number=2,
        speaker_id="alex",
        speaker_name="Alex",
        text="We are currently blocked by the connection pool settings. It is a major blocker.",
        start_time=4.0,
        end_time=8.0,
        duration=4.0,
    )
    tagged_b = tagger.tag_turn(b_turn)
    assert tagged_b.is_blocker is True
    assert "blocker" in tagged_b.signals

    # Commitment detection
    c_turn = SpeakerTurn(
        turn_id="mtg_test_turn_003",
        meeting_id="test",
        turn_number=3,
        speaker_id="alex",
        speaker_name="Alex",
        text="I will update the database configuration by tomorrow morning.",
        start_time=9.0,
        end_time=13.0,
        duration=4.0,
    )
    tagged_c = tagger.tag_turn(c_turn)
    assert tagged_c.is_commitment is True
    assert "commitment" in tagged_c.signals


def test_level2_topic_segmenter():
    normalizer = SpeakerNormalizer()
    norm_result = normalizer.normalize(SAMPLE_PAYLOAD)

    tagger = SignalTagger()
    tagged_turns = tagger.tag_turns(norm_result.turns)

    segmenter = TopicSegmenter()
    segmenter.min_turns, segmenter.max_turns = 2, 6
    segments = asyncio.run(segmenter.segment_transcript_async(
        meeting_id=SAMPLE_PAYLOAD.meeting_id,
        turns=tagged_turns,
    ))

    assert len(segments) >= 1
    for seg in segments:
        assert seg.segment_id.startswith(f"mtg_{SAMPLE_PAYLOAD.meeting_id}_seg_")
        assert seg.turns_count == len(seg.turn_ids)
        assert seg.start_time <= seg.end_time
        assert seg.title is not None
        assert seg.topic_summary is not None


def test_level3_meeting_summarizer():
    normalizer = SpeakerNormalizer()
    norm_result = normalizer.normalize(SAMPLE_PAYLOAD)

    tagger = SignalTagger()
    tagged_turns = tagger.tag_turns(norm_result.turns)

    segmenter = TopicSegmenter()
    segments = asyncio.run(segmenter.segment_transcript_async(SAMPLE_PAYLOAD.meeting_id, tagged_turns))

    summarizer = MeetingSummarizer()
    summary = summarizer.generate_intelligence(
        meeting_id=SAMPLE_PAYLOAD.meeting_id,
        turns=tagged_turns,
        segments=segments,
        title=SAMPLE_PAYLOAD.title,
    )

    # Validate ID convention
    assert summary.summary_id == f"mtg_{SAMPLE_PAYLOAD.meeting_id}_sum"
    assert summary.overview is not None
    assert len(summary.participants) >= 2

    # Validate Grounding: Every decision, commitment, and blocker must cite valid Level 1 turn IDs
    valid_turn_ids = {t.turn_id for t in tagged_turns}

    for decision in summary.decisions:
        assert len(decision.source_chunk_ids) > 0
        for chunk_id in decision.source_chunk_ids:
            assert chunk_id in valid_turn_ids

    for commitment in summary.commitments:
        assert len(commitment.source_chunk_ids) > 0
        for chunk_id in commitment.source_chunk_ids:
            assert chunk_id in valid_turn_ids

    for blocker in summary.blockers:
        assert len(blocker.source_chunk_ids) > 0
        for chunk_id in blocker.source_chunk_ids:
            assert chunk_id in valid_turn_ids


def test_api_process_full_pipeline():
    res = client.post("/api/v1/transcript/process-full", json=SAMPLE_PAYLOAD.model_dump())
    assert res.status_code == 200

    data = res.json()
    assert data["meeting_id"] == SAMPLE_PAYLOAD.meeting_id
    assert "level1_turns" in data
    assert "level2_segments" in data
    assert "level3_summary" in data

    # Verify Level 1 Turn ID convention & signals
    first_turn = data["level1_turns"][0]
    assert first_turn["turn_id"].startswith(f"mtg_{SAMPLE_PAYLOAD.meeting_id}_turn_")
    assert first_turn["is_question"] is True

    # Verify Level 2 Segment ID convention
    first_seg = data["level2_segments"][0]
    assert first_seg["segment_id"].startswith(f"mtg_{SAMPLE_PAYLOAD.meeting_id}_seg_")

    # Verify Level 3 Summary ID convention & grounding
    summary = data["level3_summary"]
    assert summary["summary_id"] == f"mtg_{SAMPLE_PAYLOAD.meeting_id}_sum"
    assert len(summary["commitments"]) > 0
    assert summary["commitments"][0]["source_chunk_ids"][0].startswith(f"mtg_{SAMPLE_PAYLOAD.meeting_id}_turn_")


def test_written_overview_only_when_requested_and_falls_back(monkeypatch):
    from engine.api.routes import _has_native_summary

    assert _has_native_summary({"overview": "Done"}) and _has_native_summary("text")
    assert not _has_native_summary(None) and not _has_native_summary({"overview": " ", "gist": None})

    summarizer = MeetingSummarizer()
    summarizer.settings = summarizer.settings.model_copy(update={"GEMINI_API_KEY": "fake"})
    turns = SpeakerNormalizer().normalize(SAMPLE_PAYLOAD).turns
    calls = []
    monkeypatch.setattr(summarizer, "_write_overview", lambda *a: calls.append(1) or "Written overview.")

    plain = summarizer.generate_intelligence(SAMPLE_PAYLOAD.meeting_id, turns)
    assert not calls and plain.overview != "Written overview."

    assert summarizer.generate_intelligence(SAMPLE_PAYLOAD.meeting_id, turns, write_overview=True).overview == "Written overview."

    def boom(*a):
        raise TimeoutError("gateway")

    monkeypatch.setattr(summarizer, "_write_overview", boom)
    assert summarizer.generate_intelligence(SAMPLE_PAYLOAD.meeting_id, turns, write_overview=True).overview == plain.overview
