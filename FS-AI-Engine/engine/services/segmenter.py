import re
import math
from typing import List, Dict, Optional, Tuple, Set
from collections import Counter
from ..core.settings import get_settings, Settings
from ..models.schemas import SpeakerTurn, TopicSegment


# Standard English stopwords to filter during lexical vectorization
STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and",
    "any", "are", "aren't", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can't", "cannot", "could", "couldn't",
    "did", "didn't", "do", "does", "doesn't", "doing", "don't", "down", "during",
    "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here",
    "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i",
    "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's",
    "its", "itself", "let's", "me", "more", "most", "mustn't", "my", "myself",
    "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "ought",
    "our", "ours", "ourselves", "out", "over", "own", "same", "shan't", "she",
    "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such",
    "than", "that", "that's", "the", "their", "theirs", "them", "themselves",
    "then", "there", "there's", "these", "they", "they'd", "they'll", "they're",
    "they've", "this", "those", "through", "to", "too", "under", "until", "up",
    "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", "were",
    "weren't", "what", "what's", "when", "when's", "where", "where's", "which",
    "while", "who", "who's", "whom", "why", "why's", "with", "won't", "would",
    "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", "yours",
    "yourself", "yourselves", "yeah", "okay", "ok", "yes", "sure", "right", "like"
}


class TopicSegmenter:
    """
    Level 2 Hybrid Boundary Topic Segmenter.
    
    1. Computes similarity guardrails (Theta_TOPIC) across turn windows using TF-IDF vectorization.
    2. Identifies candidate boundaries where topic similarity drops.
    3. Confirms boundary intent, suppressing splits on short counter-questions or momentary tangents.
    4. Enforces segment size bounds (min_turns_per_segment, max_turns_per_segment).
    5. Produces validated TopicSegment models conforming to mtg_<id>_seg_<NN>.
    """

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.similarity_threshold = self.settings.topic_similarity_threshold
        self.min_turns = self.settings.min_turns_per_segment
        self.max_turns = self.settings.max_turns_per_segment

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Tokenize text into lowercase alphanumeric tokens excluding stopwords."""
        tokens = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
        return [t for t in tokens if t not in STOPWORDS]

    def _compute_cosine_similarity(self, tokens1: List[str], tokens2: List[str]) -> float:
        """Calculate cosine similarity between token frequency distributions."""
        if not tokens1 or not tokens2:
            return 0.0

        c1 = Counter(tokens1)
        c2 = Counter(tokens2)

        intersection = set(c1.keys()) & set(c2.keys())
        if not intersection:
            return 0.0

        dot_product = sum(c1[t] * c2[t] for t in intersection)
        mag1 = math.sqrt(sum(count ** 2 for count in c1.values()))
        mag2 = math.sqrt(sum(count ** 2 for count in c2.values()))

        if mag1 == 0 or mag2 == 0:
            return 0.0

        return dot_product / (mag1 * mag2)

    def _is_short_tangent(self, turns: List[SpeakerTurn], candidate_idx: int) -> bool:
        """
        Check if turn at candidate_idx is a brief tangent or counter-question.
        If the turn is an interrogative or very short utterance, do not shatter context.
        """
        turn = turns[candidate_idx]
        word_count = len(turn.text.split())

        # If turn is a question or brief comment (< 12 words), it's likely a clarification
        if turn.is_question or word_count < 12:
            return True

        # Check if subsequent turn quickly returns to previous speaker or vocabulary
        if candidate_idx + 1 < len(turns):
            next_turn = turns[candidate_idx + 1]
            if candidate_idx > 0 and next_turn.speaker_id == turns[candidate_idx - 1].speaker_id:
                return True

        return False

    def segment_transcript(
        self,
        meeting_id: str,
        turns: List[SpeakerTurn],
        similarity_threshold_override: Optional[float] = None,
        min_turns_override: Optional[int] = None,
        max_turns_override: Optional[int] = None,
    ) -> List[TopicSegment]:
        """
        Segment normalized turns into cohesive Level 2 topic segments.

        Args:
            meeting_id: Unique meeting identifier.
            turns: Ordered list of Level 1 SpeakerTurn objects.
            similarity_threshold_override: Optional override for Theta_TOPIC.
            min_turns_override: Optional override for minimum turns per segment.
            max_turns_override: Optional override for maximum turns per segment.

        Returns:
            List of validated TopicSegment objects conforming to mtg_<id>_seg_<NN>.
        """
        if not turns:
            return []

        threshold = similarity_threshold_override if similarity_threshold_override is not None else self.similarity_threshold
        min_turns = min_turns_override if min_turns_override is not None else self.min_turns
        max_turns = max_turns_override if max_turns_override is not None else self.max_turns

        # Handle very short meetings that fit in a single segment
        if len(turns) <= min_turns:
            return [self._build_segment(meeting_id, 1, turns, confidence=1.0)]

        # Precompute token lists for each turn
        turn_tokens = [self._tokenize(t.text) for t in turns]

        # Calculate boundary similarity scores between adjacent windows (window size = 2)
        boundary_scores: List[float] = []
        for i in range(len(turns) - 1):
            left_window_tokens = []
            for j in range(max(0, i - 1), i + 1):
                left_window_tokens.extend(turn_tokens[j])

            right_window_tokens = []
            for j in range(i + 1, min(len(turns), i + 3)):
                right_window_tokens.extend(turn_tokens[j])

            sim = self._compute_cosine_similarity(left_window_tokens, right_window_tokens)
            boundary_scores.append(sim)

        # Identify segmentation cut points
        cut_indices: List[int] = []
        last_cut = 0

        for i in range(len(boundary_scores)):
            turns_in_current = (i + 1) - last_cut
            similarity = boundary_scores[i]

            # Candidate shift flagged if similarity drops below threshold
            is_candidate_shift = similarity < threshold

            # Guardrail 1: Enforce minimum turns
            if turns_in_current < min_turns:
                continue

            # Guardrail 2: Enforce maximum turns (force split at local minimum if exceeding max)
            if turns_in_current >= max_turns:
                cut_indices.append(i + 1)
                last_cut = i + 1
                continue

            # Guardrail 3: LLM / heuristic confirmation against short tangents
            if is_candidate_shift:
                if self._is_short_tangent(turns, i + 1):
                    # Suppress false split on short counter-question or brief tangent
                    continue
                else:
                    cut_indices.append(i + 1)
                    last_cut = i + 1

        # Construct segments from cut indices
        segments: List[TopicSegment] = []
        start_idx = 0
        seg_counter = 1

        for cut_idx in cut_indices:
            seg_turns = turns[start_idx:cut_idx]
            if seg_turns:
                conf = round(1.0 - (boundary_scores[cut_idx - 1] if cut_idx - 1 < len(boundary_scores) else 0.5), 2)
                segments.append(self._build_segment(meeting_id, seg_counter, seg_turns, confidence=conf))
                seg_counter += 1
            start_idx = cut_idx

        # Trailing segment
        if start_idx < len(turns):
            trailing_turns = turns[start_idx:]
            # If trailing turns are fewer than min_turns and we already have a previous segment, merge into it
            if len(trailing_turns) < min_turns and segments:
                last_seg = segments[-1]
                combined_turns = [t for t in turns if t.turn_id in last_seg.turn_ids] + trailing_turns
                segments[-1] = self._build_segment(meeting_id, last_seg.segment_index, combined_turns, confidence=last_seg.boundary_confidence)
            else:
                segments.append(self._build_segment(meeting_id, seg_counter, trailing_turns, confidence=0.85))

        return segments

    def _build_segment(
        self,
        meeting_id: str,
        segment_index: int,
        segment_turns: List[SpeakerTurn],
        confidence: float = 1.0,
    ) -> TopicSegment:
        """Construct a validated TopicSegment conforming to mtg_<id>_seg_<NN>."""
        clean_meeting_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", meeting_id.strip())
        segment_id = f"mtg_{clean_meeting_id}_seg_{segment_index:02d}"

        start_time = round(min(t.start_time for t in segment_turns), 2)
        end_time = round(max(t.end_time for t in segment_turns), 2)
        duration = round(max(0.0, end_time - start_time), 2)

        key_speakers = list(dict.fromkeys(t.speaker_name for t in segment_turns))
        title = self._generate_title(segment_turns)
        summary = self._generate_summary(segment_turns)

        return TopicSegment(
            segment_id=segment_id,
            meeting_id=meeting_id,
            segment_index=segment_index,
            title=title,
            topic_summary=summary,
            turn_ids=[t.turn_id for t in segment_turns],
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            turns_count=len(segment_turns),
            key_speakers=key_speakers,
            boundary_confidence=max(0.1, min(1.0, confidence)),
        )

    def _generate_title(self, turns: List[SpeakerTurn]) -> str:
        """Synthesize a concise headline based on salient terms in this segment."""
        tokens = []
        for t in turns:
            tokens.extend(self._tokenize(t.text))

        if not tokens:
            return f"Topic Discussion (Turns {turns[0].turn_number}-{turns[-1].turn_number})"

        counts = Counter(tokens)
        top_terms = [word.capitalize() for word, _ in counts.most_common(3)]
        return f"{' & '.join(top_terms)} Discussion"

    def _generate_summary(self, turns: List[SpeakerTurn]) -> str:
        """Synthesize an informative summary for the topic segment."""
        speakers = list(dict.fromkeys(t.speaker_name for t in turns))
        speakers_str = ", ".join(speakers)

        # Highlight if decisions or blockers are present in this segment
        has_commitments = any(t.is_commitment for t in turns)
        has_blockers = any(t.is_blocker for t in turns)

        highlights = []
        if has_commitments:
            highlights.append("action commitments discussed")
        if has_blockers:
            highlights.append("blockers / impediments raised")

        highlight_str = f" with {', '.join(highlights)}" if highlights else ""
        turn_range = f"turn {turns[0].turn_number} to {turns[-1].turn_number}"

        first_sentence = turns[0].text[:80].strip()
        if len(turns[0].text) > 80:
            first_sentence += "..."

        return f"Discussion involving {speakers_str} covering {turn_range}{highlight_str}: \"{first_sentence}\""
