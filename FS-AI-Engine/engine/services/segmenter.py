import asyncio
import logging
import math
import re
from collections import Counter

from ..core.config import Settings, get_settings
from ..models.schemas import SpeakerTurn, TopicSegment
from .llm_client import OpenRouterClient, openrouter_client

logger = logging.getLogger(__name__)

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
    5. Leverages LLM to synthesize concise, informative 5-to-6 word executive headings for each topic.
    6. Produces validated TopicSegment models conforming to mtg_<id>_seg_<NN>.
    """

    def __init__(self, settings: Settings | None = None, llm_client: OpenRouterClient | None = None):
        self.settings = settings or get_settings()
        self.llm_client = llm_client or openrouter_client
        self.similarity_threshold = self.settings.topic_similarity_threshold
        self.min_turns = self.settings.min_turns_per_segment
        self.max_turns = self.settings.max_turns_per_segment
        self.llm_model = getattr(self.settings, "LLM_MODEL_NAME", None) or getattr(self.settings, "llm_model", "gemini-2.5-flash")

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Tokenize text into lowercase alphanumeric tokens excluding stopwords."""
        tokens = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
        return [t for t in tokens if t not in STOPWORDS]

    def _compute_cosine_similarity(self, tokens1: list[str], tokens2: list[str]) -> float:
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

    def _is_short_tangent(self, turns: list[SpeakerTurn], candidate_idx: int) -> bool:
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
        turns: list[SpeakerTurn],
        similarity_threshold_override: float | None = None,
        min_turns_override: int | None = None,
        max_turns_override: int | None = None,
    ) -> list[TopicSegment]:
        """Synchronous wrapper around segment_transcript_async (for non-async callers)."""
        return asyncio.run(self.segment_transcript_async(
            meeting_id, turns, similarity_threshold_override, min_turns_override, max_turns_override,
        ))

    async def segment_transcript_async(
        self,
        meeting_id: str,
        turns: list[SpeakerTurn],
        similarity_threshold_override: float | None = None,
        min_turns_override: int | None = None,
        max_turns_override: int | None = None,
    ) -> list[TopicSegment]:
        """
        Asynchronously segment turns into cohesive Level 2 topic segments with LLM-generated executive headings.
        """
        if not turns:
            return []

        threshold = similarity_threshold_override if similarity_threshold_override is not None else self.similarity_threshold
        min_turns = min_turns_override if min_turns_override is not None else self.min_turns
        max_turns = max_turns_override if max_turns_override is not None else self.max_turns

        if len(turns) <= min_turns:
            return [await self._build_segment_async(meeting_id, 1, turns, confidence=1.0)]

        turn_tokens = [self._tokenize(t.text) for t in turns]

        boundary_scores: list[float] = []
        for i in range(len(turns) - 1):
            left_window_tokens = []
            for j in range(max(0, i - 1), i + 1):
                left_window_tokens.extend(turn_tokens[j])

            right_window_tokens = []
            for j in range(i + 1, min(len(turns), i + 3)):
                right_window_tokens.extend(turn_tokens[j])

            sim = self._compute_cosine_similarity(left_window_tokens, right_window_tokens)
            boundary_scores.append(sim)

        cut_indices: list[int] = []
        last_cut = 0

        for i in range(len(boundary_scores)):
            turns_in_current = (i + 1) - last_cut
            similarity = boundary_scores[i]
            is_candidate_shift = similarity < threshold

            if turns_in_current < min_turns:
                continue

            if turns_in_current >= max_turns:
                cut_indices.append(i + 1)
                last_cut = i + 1
                continue

            if is_candidate_shift:
                if self._is_short_tangent(turns, i + 1):
                    continue
                else:
                    cut_indices.append(i + 1)
                    last_cut = i + 1

        # Collect segment boundaries and turn slices
        segment_defs = []
        start_idx = 0
        seg_counter = 1

        for cut_idx in cut_indices:
            seg_turns = turns[start_idx:cut_idx]
            if seg_turns:
                conf = round(1.0 - (boundary_scores[cut_idx - 1] if cut_idx - 1 < len(boundary_scores) else 0.5), 2)
                segment_defs.append((seg_counter, seg_turns, conf))
                seg_counter += 1
            start_idx = cut_idx

        if start_idx < len(turns):
            trailing_turns = turns[start_idx:]
            if len(trailing_turns) < min_turns and segment_defs:
                last_idx, last_turns, last_conf = segment_defs[-1]
                combined = last_turns + trailing_turns
                segment_defs[-1] = (last_idx, combined, last_conf)
            else:
                segment_defs.append((seg_counter, trailing_turns, 0.85))

        # Build segments concurrently with bounded semaphore (6 workers) for high throughput
        sem = asyncio.Semaphore(6)

        async def _build_bounded(s_idx: int, s_turns: list[SpeakerTurn], s_conf: float) -> TopicSegment:
            async with sem:
                return await self._build_segment_async(meeting_id, s_idx, s_turns, confidence=s_conf)

        segments = await asyncio.gather(*[
            _build_bounded(s_idx, s_turns, s_conf)
            for s_idx, s_turns, s_conf in segment_defs
        ])

        return list(segments)

    async def _build_segment_async(
        self,
        meeting_id: str,
        segment_index: int,
        segment_turns: list[SpeakerTurn],
        confidence: float = 1.0,
    ) -> TopicSegment:
        """Construct a validated TopicSegment with async LLM heading generation."""
        clean_meeting_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", meeting_id.strip())
        segment_id = f"mtg_{clean_meeting_id}_seg_{segment_index:02d}"

        start_time = round(min(t.start_time for t in segment_turns), 2)
        end_time = round(max(t.end_time for t in segment_turns), 2)
        duration = round(max(0.0, end_time - start_time), 2)

        key_speakers = list(dict.fromkeys(t.speaker_name for t in segment_turns))
        title = await self._generate_title_async(segment_turns)
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

    async def _generate_title_async(self, turns: list[SpeakerTurn]) -> str:
        """
        Synthesize an understandable, executive-level 5-to-6 word topic heading summarizing the discussion.
        """
        sample_text = "\n".join([f"{t.speaker_name}: {t.text}" for t in turns])[:1000]
        prompt = (
            "You are an executive meeting analyst. Analyze the following meeting dialogue excerpt and generate an understandable, professional 5-to-6 word topic heading that describes what was discussed.\n\n"
            "STRICT RULES:\n"
            "1. Length: Exactly 5 to 6 words.\n"
            "2. Quality: Must convey a clear, meaningful business or technical topic (e.g., "
            "'Implementing Hybrid Search And Database Caching', 'Resolving Staging Contention And Timeout Errors', "
            "'Reviewing Frontend UI Migration And Roadmap').\n"
            "3. Format: Return ONLY the title itself. Do NOT include quotes, prefixes like 'Topic:', or generic suffixes like 'Discussion' or 'Chapter'.\n"
            "4. Forbidden: NEVER use vague word lists like 'Word & Word & Word'.\n\n"
            f"Meeting Excerpt:\n{sample_text}\n\n"
            "Topic Heading:"
        )
        try:
            raw_title = await asyncio.wait_for(
                self.llm_client.generate_chat_completion(
                    messages=[{"role": "user", "content": prompt}],
                    model=self.llm_model,
                    max_tokens=30,
                    temperature=0.2,
                ),
                timeout=10.0,
            )
            cleaned = raw_title.strip().strip('"').strip("'").strip()
            cleaned = re.sub(r"^(Topic Heading|Title|Topic|Chapter):\s*", "", cleaned, flags=re.IGNORECASE).strip()
            cleaned = cleaned.strip('"').strip("'").strip(".").strip()
            words = cleaned.split()
            if 3 <= len(words) <= 10:
                if len(words) > 6:
                    cleaned = " ".join(words[:6])
                return cleaned
        except Exception as exc:
            logger.warning(f"LLM topic title generation failed ({exc}). Using heuristic summary title.")

        return self._generate_fallback_title(turns)

    def _generate_fallback_title(self, turns: list[SpeakerTurn]) -> str:
        """Generate a meaningful 5-to-6 word descriptive heading without vague 'X & Y & Z' formatting."""
        # Find first substantive turn with an action, commitment, or blocker
        for t in turns:
            if getattr(t, "is_blocker", False) or getattr(t, "is_commitment", False) or len(t.text.split()) >= 6:
                words = re.findall(r"\b[A-Za-z0-9\-_]{3,}\b", t.text)
                meaningful = [w.capitalize() for w in words if w.lower() not in STOPWORDS]
                if len(meaningful) >= 4:
                    action = "Addressing" if getattr(t, "is_blocker", False) else ("Planning" if getattr(t, "is_commitment", False) else "Reviewing")
                    return f"{action} {' '.join(meaningful[:5])}"

        # Default fallback from most salient terms
        tokens = []
        for t in turns:
            tokens.extend(self._tokenize(t.text))
        counts = Counter(tokens)
        top = [w.capitalize() for w, _ in counts.most_common(5) if w.lower() not in STOPWORDS]
        if top:
            return f"Discussion On {' '.join(top[:4])}"
        return f"Meeting Discussion For Turns {turns[0].turn_number} To {turns[-1].turn_number}"

    def _generate_summary(self, turns: list[SpeakerTurn]) -> str:
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
