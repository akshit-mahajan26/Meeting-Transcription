import re
import logging
from typing import List, Dict, Any, Optional
import tiktoken
from ..core.config import settings, Settings
from ..models.schemas import SpeakerTurn

logger = logging.getLogger(__name__)


class TokenAwareChunker:
    """
    Token-Aware Chunking & Sub-Splitter Service.
    
    1. Measures token length of each speaker turn using tiktoken corresponding to settings.embedding_model.
    2. Sub-splits atomic turns exceeding settings.max_turn_tokens (default 400 tokens).
    3. Maintains a 1-to-2 sentence overlap (settings.chunk_overlap_sentences) between consecutive sub-chunks.
    4. Generates deterministic chunk IDs following [meeting_id]_turn_[sequence_number].
    """

    def __init__(self, config: Optional[Settings] = None):
        self.config = config or settings
        self.max_tokens = self.config.max_turn_tokens
        self.overlap_sentences = self.config.chunk_overlap_sentences
        self.embedding_model = self.config.embedding_model

        # Dynamically load tokenizer encoding matching configured embedding model
        self.tokenizer = None
        try:
            self.tokenizer = tiktoken.encoding_for_model(self.embedding_model)
        except Exception:
            try:
                self.tokenizer = tiktoken.get_encoding("cl100k_base")
            except Exception as exc:
                logger.warning(
                    f"Unable to load tiktoken encoding for '{self.embedding_model}' ({exc}). Using word-ratio fallback."
                )
                self.tokenizer = None

    def count_tokens(self, text: str) -> int:
        """Count tokens in text string using configured model tokenizer or heuristic fallback."""
        if not text:
            return 0
        if self.tokenizer:
            try:
                return len(self.tokenizer.encode(text))
            except Exception:
                pass
        # Standard NLP heuristic: 1 word ≈ 1.33 tokens
        return int(len(text.split()) * 1.33)

    @staticmethod
    def split_into_sentences(text: str) -> List[str]:
        """Split text into sentences using punctuation boundaries."""
        sentences = re.split(r"(?<=[.?!])\s+", text.strip())
        return [s.strip() for s in sentences if s.strip()]

    def process_turns(self, meeting_id: str, turns: List[SpeakerTurn]) -> List[SpeakerTurn]:
        """
        Process a list of speaker turns, sub-splitting any turn exceeding max_turn_tokens
        while preserving sentence overlap and generating deterministic IDs.

        Args:
            meeting_id: Unique meeting identifier.
            turns: List of Level 1 SpeakerTurn objects.

        Returns:
            List of SpeakerTurn objects (where oversized turns have been sub-split).
        """
        clean_meeting_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", meeting_id.strip())
        if not clean_meeting_id.startswith("mtg_"):
            clean_meeting_id = f"mtg_{clean_meeting_id}"

        output_chunks: List[SpeakerTurn] = []
        sequence_number = 1

        for turn in turns:
            token_count = self.count_tokens(turn.text)

            if token_count <= self.max_tokens:
                # Turn does not exceed threshold; retain with deterministic sequence ID
                deterministic_id = f"{clean_meeting_id}_turn_{sequence_number:03d}"
                turn_copy = turn.model_copy(update={
                    "turn_id": deterministic_id,
                    "turn_number": sequence_number,
                })
                output_chunks.append(turn_copy)
                sequence_number += 1
            else:
                # Turn exceeds token threshold; sub-split with sentence overlap
                sub_chunks = self._sub_split_turn(clean_meeting_id, turn, sequence_number)
                output_chunks.extend(sub_chunks)
                sequence_number += len(sub_chunks)

        return output_chunks

    @staticmethod
    def format_timestamp(seconds: Optional[float]) -> str:
        """Format seconds into HH:MM:SS string."""
        if seconds is None:
            return "00:00:00"
        total_sec = max(0, int(round(float(seconds))))
        hrs = total_sec // 3600
        mins = (total_sec % 3600) // 60
        secs = total_sec % 60
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"

    @classmethod
    def enrich_turn_text(cls, speaker: str, timestamp: str, text: str) -> str:
        """
        Enrich chunk text by prefixing speaker and timestamp context:
        f"Speaker: {speaker} | Timestamp: {timestamp} | Content: {text}"
        """
        spk = (speaker or "Unknown").strip()
        ts = (timestamp or "00:00:00").strip()
        txt = (text or "").strip()
        return f"Speaker: {spk} | Timestamp: {ts} | Content: {txt}"

    def prepare_enriched_chunks(
        self,
        meeting_id: str,
        turns: List[SpeakerTurn],
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Processes turns into token-aware sub-chunks and formats each with enriched metadata
        ready for vector embedding.
        """
        chunked = self.process_turns(meeting_id, turns)
        results: List[Dict[str, Any]] = []
        eff_proj = project_id or "default-project"
        for t in chunked:
            ts_str = self.format_timestamp(t.start_time)
            enriched = self.enrich_turn_text(t.speaker_name, ts_str, t.text)
            results.append({
                "turn": t,
                "enriched_text": enriched,
                "metadata": {
                    "speaker": t.speaker_name,
                    "timestamp": ts_str,
                    "meeting_id": meeting_id,
                    "project_id": eff_proj,
                    "turn_id": t.turn_id,
                }
            })
        return results

    def _sub_split_turn(
        self,
        clean_meeting_id: str,
        turn: SpeakerTurn,
        start_sequence: int,
    ) -> List[SpeakerTurn]:
        """
        Sub-split a single turn into consecutive overlapping chunks.
        Ensures 1-to-2 sentence overlap between sub-chunks.
        """
        sentences = self.split_into_sentences(turn.text)
        if not sentences:
            # Fallback if no sentence boundaries found
            deterministic_id = f"{clean_meeting_id}_turn_{start_sequence:03d}"
            return [turn.model_copy(update={"turn_id": deterministic_id, "turn_number": start_sequence})]

        sub_chunks: List[SpeakerTurn] = []
        current_sentences: List[str] = []
        current_token_count = 0
        seq = start_sequence
        total_duration = turn.duration
        total_chars = max(1, len(turn.text))

        idx = 0
        while idx < len(sentences):
            sentence = sentences[idx]
            sentence_tokens = self.count_tokens(sentence)

            # If adding this sentence exceeds max_tokens and we already have sentences
            if current_sentences and (current_token_count + sentence_tokens > self.max_tokens):
                # Finalize current sub-chunk
                sub_text = " ".join(current_sentences)
                sub_turn = self._create_sub_turn(
                    clean_meeting_id=clean_meeting_id,
                    parent_turn=turn,
                    sequence_number=seq,
                    sub_text=sub_text,
                    total_chars=total_chars,
                    total_duration=total_duration,
                )
                sub_chunks.append(sub_turn)
                seq += 1

                # Carry over overlap sentences for context continuity
                overlap_count = min(self.overlap_sentences, len(current_sentences))
                overlap_sentences = current_sentences[-overlap_count:] if overlap_count > 0 else []
                current_sentences = list(overlap_sentences)
                current_token_count = sum(self.count_tokens(s) for s in current_sentences)

            current_sentences.append(sentence)
            current_token_count += sentence_tokens
            idx += 1

        # Finalize remaining sentences
        if current_sentences:
            sub_text = " ".join(current_sentences)
            sub_turn = self._create_sub_turn(
                clean_meeting_id=clean_meeting_id,
                parent_turn=turn,
                sequence_number=seq,
                sub_text=sub_text,
                total_chars=total_chars,
                total_duration=total_duration,
            )
            sub_chunks.append(sub_turn)

        return sub_chunks

    def _create_sub_turn(
        self,
        clean_meeting_id: str,
        parent_turn: SpeakerTurn,
        sequence_number: int,
        sub_text: str,
        total_chars: int,
        total_duration: float,
    ) -> SpeakerTurn:
        """Create a proportional child turn representing a sub-chunk."""
        # Calculate proportional start/end time based on character offset
        char_ratio = min(1.0, len(sub_text) / total_chars)
        sub_duration = round(max(0.5, total_duration * char_ratio), 2)
        deterministic_id = f"{clean_meeting_id}_turn_{sequence_number:03d}"

        return SpeakerTurn(
            turn_id=deterministic_id,
            meeting_id=parent_turn.meeting_id,
            turn_number=sequence_number,
            speaker_id=parent_turn.speaker_id,
            speaker_name=parent_turn.speaker_name,
            text=sub_text,
            start_time=parent_turn.start_time,
            end_time=round(parent_turn.start_time + sub_duration, 2),
            duration=sub_duration,
            sentence_indices=parent_turn.sentence_indices,
            is_question=parent_turn.is_question,
            is_commitment=parent_turn.is_commitment,
            is_blocker=parent_turn.is_blocker,
            signals=parent_turn.signals,
        )
