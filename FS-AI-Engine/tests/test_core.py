import os
import sys

# Add service paths to sys.path
sys.path.insert(0, os.path.abspath("FS-AI-Engine"))
sys.path.insert(0, os.path.abspath("FS-backend"))

from engine.models.schemas import RawSentence, RawTranscriptPayload
from engine.services.normalizer import SpeakerNormalizer


def test_turn_id_generation():
    normalizer = SpeakerNormalizer()
    turn_id = normalizer.generate_turn_id("m101", 1)
    assert turn_id == "mtg_m101_turn_001"

    turn_id_large = normalizer.generate_turn_id("team-sync", 42)
    assert turn_id_large == "mtg_team-sync_turn_042"


def test_speaker_normalizer_merges_consecutive_utterances():
    normalizer = SpeakerNormalizer()
    payload = RawTranscriptPayload(
        meeting_id="meet_123",
        title="Weekly Sync",
        sentences=[
            RawSentence(
                index=0,
                speaker_name="Alice",
                speaker_id="alice_1",
                text="Hello everyone.",
                start_time=0.0,
                end_time=2.0,
            ),
            RawSentence(
                index=1,
                speaker_name="Alice",
                speaker_id="alice_1",
                text="Welcome to the sprint retro.",
                start_time=2.1,
                end_time=5.0,
            ),
            RawSentence(
                index=2,
                speaker_name="Bob",
                speaker_id="bob_1",
                text="Thanks Alice! Glad to be here.",
                start_time=5.5,
                end_time=8.0,
            ),
            RawSentence(
                index=3,
                speaker_name="Alice",
                speaker_id="alice_1",
                text="Let's review the blockers.",
                start_time=8.5,
                end_time=11.0,
            ),
        ],
    )

    result = normalizer.normalize(payload)

    # 4 raw sentences by Alice (x2), Bob, Alice -> should result in exactly 3 turns
    assert result.total_raw_sentences == 4
    assert result.total_normalized_turns == 3
    assert len(result.turns) == 3

    # Turn 1: Alice merged
    turn1 = result.turns[0]
    assert turn1.turn_id == "mtg_meet_123_turn_001"
    assert turn1.turn_number == 1
    assert turn1.speaker_name == "Alice"
    assert turn1.text == "Hello everyone. Welcome to the sprint retro."
    assert turn1.start_time == 0.0
    assert turn1.end_time == 5.0
    assert turn1.sentence_indices == [0, 1]

    # Turn 2: Bob
    turn2 = result.turns[1]
    assert turn2.turn_id == "mtg_meet_123_turn_002"
    assert turn2.turn_number == 2
    assert turn2.speaker_name == "Bob"
    assert turn2.text == "Thanks Alice! Glad to be here."
    assert turn2.start_time == 5.5
    assert turn2.end_time == 8.0
    assert turn2.sentence_indices == [2]

    # Turn 3: Alice again
    turn3 = result.turns[2]
    assert turn3.turn_id == "mtg_meet_123_turn_003"
    assert turn3.turn_number == 3
    assert turn3.speaker_name == "Alice"
    assert turn3.text == "Let's review the blockers."
    assert turn3.start_time == 8.5
    assert turn3.end_time == 11.0
    assert turn3.sentence_indices == [3]


def test_meeting_dates_are_formatted_and_offered_to_the_prompt():
    from engine.graphs.chat_records import build_meeting_details, format_meeting_date

    assert format_meeting_date("1780383600000") == "2026-06-02 07:00 UTC"
    assert format_meeting_date("2026-06-02T07:00:00Z") == "2026-06-02 07:00 UTC"
    assert format_meeting_date(None) is None and format_meeting_date("0") is None

    meetings = [
        {"id": "m1", "title": "Standup", "project_id": "p", "date": "2026-06-02 07:00 UTC"},
        {"id": "m2", "title": "Retro", "project_id": None, "date": None},
    ]
    block = build_meeting_details(meetings, ["m1", "m2", "m1", "missing"])
    assert "Held on: 2026-06-02 07:00 UTC" in block
    assert "Held on: not recorded" in block
    assert block.count("Standup") == 1
    assert build_meeting_details(meetings, ["missing"]) == ""
