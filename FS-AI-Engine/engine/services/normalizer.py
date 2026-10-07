import re
from typing import List, Optional
from ..models.schemas import (
    RawSentence,
    RawTranscriptPayload,
    SpeakerTurn,
    NormalizationResult,
)


class SpeakerNormalizer:
    """
    Speaker Normalization Engine for Meeting Transcripts.
    
    Responsibilities:
    1. Sorts input sentences chronologically.
    2. Merges consecutive utterances spoken by the same speaker.
    3. Computes accurate turn boundaries (start_time, end_time, duration).
    4. Enforces strict deterministic turn IDs conforming to mtg_<id>_turn_<NNN>.
    """

    @staticmethod
    def _sanitize_id(identifier: str) -> str:
        """Sanitize meeting identifier to safe characters for ID formatting."""
        # Replace non-alphanumeric (except dashes and underscores) with underscores
        sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", identifier.strip())
        return sanitized or "default"

    @classmethod
    def generate_turn_id(cls, meeting_id: str, turn_number: int) -> str:
        """
        Generate deterministic turn ID conforming to mtg_<id>_turn_<NNN>.
        Guarantees minimum 3-digit zero-padding.
        
        Example:
            mtg_12345_turn_001
        """
        clean_meeting_id = cls._sanitize_id(meeting_id)
        return f"mtg_{clean_meeting_id}_turn_{turn_number:03d}"

    def normalize(self, payload: RawTranscriptPayload) -> NormalizationResult:
        """
        Normalize a raw transcript payload into consolidated Level 1 speaker turns.

        Args:
            payload: Raw transcript payload containing sentences and metadata.

        Returns:
            NormalizationResult with consolidated turns and summary statistics.
        """
        raw_sentences = payload.sentences
        if not raw_sentences:
            return NormalizationResult(
                meeting_id=payload.meeting_id,
                title=payload.title,
                date=payload.date,
                duration=payload.duration or 0.0,
                total_raw_sentences=0,
                total_normalized_turns=0,
                turns=[],
            )

        # Sort sentences chronologically by start_time, then index
        sorted_sentences = sorted(raw_sentences, key=lambda s: (s.start_time, s.index))

        normalized_turns: List[SpeakerTurn] = []
        current_sentences: List[RawSentence] = []
        turn_counter = 1

        for sentence in sorted_sentences:
            cleaned_text = sentence.text.strip()
            if not cleaned_text:
                continue

            current_speaker_name = (sentence.speaker_name or "Unknown Speaker").strip()
            current_speaker_id = (sentence.speaker_id or current_speaker_name.lower().replace(" ", "_")).strip()

            if not current_sentences:
                # First valid sentence initializes a turn
                current_sentences.append(sentence)
                continue

            prev_sentence = current_sentences[-1]
            prev_speaker_name = (prev_sentence.speaker_name or "Unknown Speaker").strip()
            prev_speaker_id = (prev_sentence.speaker_id or prev_speaker_name.lower().replace(" ", "_")).strip()

            # Check if speaker is identical (case-insensitive name or identical speaker_id)
            same_speaker = (
                current_speaker_id == prev_speaker_id
                or current_speaker_name.lower() == prev_speaker_name.lower()
            )

            if same_speaker:
                # Merge into existing turn accumulator
                current_sentences.append(sentence)
            else:
                # Finalize current turn
                turn = self._build_speaker_turn(
                    meeting_id=payload.meeting_id,
                    turn_number=turn_counter,
                    sentences=current_sentences,
                )
                normalized_turns.append(turn)
                turn_counter += 1

                # Start new turn
                current_sentences = [sentence]

        # Finalize trailing turn if any
        if current_sentences:
            turn = self._build_speaker_turn(
                meeting_id=payload.meeting_id,
                turn_number=turn_counter,
                sentences=current_sentences,
            )
            normalized_turns.append(turn)

        total_duration = payload.duration
        if total_duration is None and normalized_turns:
            total_duration = round(normalized_turns[-1].end_time, 2)

        return NormalizationResult(
            meeting_id=payload.meeting_id,
            title=payload.title,
            date=payload.date,
            duration=total_duration,
            total_raw_sentences=len(raw_sentences),
            total_normalized_turns=len(normalized_turns),
            turns=normalized_turns,
        )

    def _build_speaker_turn(
        self,
        meeting_id: str,
        turn_number: int,
        sentences: List[RawSentence],
    ) -> SpeakerTurn:
        """Construct a validated SpeakerTurn from a sequence of sentences by one speaker."""
        primary_speaker_name = (sentences[0].speaker_name or "Unknown Speaker").strip()
        primary_speaker_id = (
            sentences[0].speaker_id or primary_speaker_name.lower().replace(" ", "_")
        ).strip()

        # Join texts cleanly
        merged_text = " ".join(s.text.strip() for s in sentences if s.text.strip())

        start_time = round(min(s.start_time for s in sentences), 2)
        end_time = round(max(s.end_time for s in sentences), 2)
        duration = round(max(0.0, end_time - start_time), 2)

        turn_id = self.generate_turn_id(meeting_id, turn_number)
        sentence_indices = [s.index for s in sentences]

        return SpeakerTurn(
            turn_id=turn_id,
            meeting_id=meeting_id,
            turn_number=turn_number,
            speaker_id=primary_speaker_id,
            speaker_name=primary_speaker_name,
            text=merged_text,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            sentence_indices=sentence_indices,
        )
