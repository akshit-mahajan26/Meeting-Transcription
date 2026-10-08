import re
import math
import logging
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field

from ..core.config import settings, Settings
from ..models.schemas import SpeakerTurn
from .embedding_service import EmbeddingService
from .llm_client import OpenRouterClient, openrouter_client

logger = logging.getLogger(__name__)


class TopicSegmentData(BaseModel):
    """Data transfer model for Level 2 Topic Segment before database insertion."""
    topic_index: int = Field(..., ge=1, description="1-based sequential index of the topic chapter")
    title: str = Field(..., description="Descriptive chapter title for the topic segment")
    compiled_text: str = Field(..., description="Compiled verbatim text block of all turns in this topic")
    start_time: float = Field(..., ge=0.0)
    end_time: float = Field(..., ge=0.0)
    turns: List[SpeakerTurn] = Field(default_factory=list)


class SemanticBoundaryDetector:
    """
    Semantic Boundary Detector & Topic Generator.
    
    1. Leverages EmbeddingService to obtain vector representations for sequential turns.
    2. Computes cosine similarity between consecutive turns.
    3. Triggers candidate topic boundaries when similarity < settings.boundary_similarity_threshold.
    4. Runs a lightweight LLM confirmation check using OpenRouterClient to filter false tangents.
    5. Groups confirmed turns into Level 2 Topic Segments (meeting_topics) with descriptive chapter titles.
    6. Compiles a Level 3 Executive Summary for the meetings record.
    """

    def __init__(
        self,
        config: Optional[Settings] = None,
        embedding_service: Optional[EmbeddingService] = None,
        llm_client: Optional[OpenRouterClient] = None,
    ):
        self.config = config or settings
        self.embedding_service = embedding_service or EmbeddingService(config=self.config)
        self.llm_client = llm_client or openrouter_client
        self.threshold = self.config.boundary_similarity_threshold
        self.llm_model = self.config.LLM_MODEL_NAME

    @staticmethod
    def cosine_similarity(v1: List[float], v2: List[float]) -> float:
        """Compute cosine similarity between two high-dimensional vectors."""
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        dot = sum(a * b for a, b in zip(v1, v2))
        norm_a = math.sqrt(sum(a * a for a in v1))
        norm_b = math.sqrt(sum(b * b for b in v2))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return max(-1.0, min(1.0, dot / (norm_a * norm_b)))

    detect_boundaries_and_topics = None # will be set
    async def detect_boundaries_and_generate_topics(
        self,
        meeting_id: str,
        turns: List[SpeakerTurn],
        title: Optional[str] = None,
        meeting_title: Optional[str] = None,
    ) -> Tuple[List[TopicSegmentData], str, List[List[float]]]:
        """
        Process an ordered sequence of speaker turns into Level 2 Topics and Level 3 Executive Summary.
        """
        if not turns:
            return [], "No conversation recorded.", []

        meeting_title = title or meeting_title or f"Meeting {meeting_id}"

        def _fmt_ts(sec: Optional[float]) -> str:
            s = max(0, int(round(float(sec or 0))))
            return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

        # 1. Generate embeddings for all turns in sequence using enriched text format
        # Format: f"Speaker: {speaker} | Timestamp: {timestamp} | Content: {text}"
        turn_texts = [
            f"Speaker: {turn.speaker_name or 'Unknown'} | Timestamp: {_fmt_ts(turn.start_time)} | Content: {turn.text or ''}"
            for turn in turns
        ]
        embeddings = await self.embedding_service.generate_embeddings(turn_texts)

        # Single turn edge case
        if len(turns) == 1:
            single_topic = TopicSegmentData(
                topic_index=1,
                title=f"{meeting_title} - General Discussion",
                compiled_text=f"{turns[0].speaker_name}: {turns[0].text}",
                start_time=turns[0].start_time,
                end_time=turns[0].end_time,
                turns=turns,
            )
            exec_summary = f"Meeting '{title}' consisted of 1 speaker turn: {turns[0].text[:120]}..."
            return [single_topic], exec_summary, embeddings

        # 2. Compute cosine similarity between consecutive turns and collect candidate shifts
        candidate_cut_indices: List[int] = []
        for i in range(len(embeddings) - 1):
            sim = self.cosine_similarity(embeddings[i], embeddings[i + 1])
            if sim < self.threshold:
                candidate_cut_indices.append(i + 1)

        # 3. LLM verification / tangent protection for candidates via OpenRouter
        confirmed_cut_indices: List[int] = []
        for cut_idx in candidate_cut_indices:
            is_shift = await self._verify_boundary_with_llm(
                turn_before=turns[cut_idx - 1],
                turn_after=turns[cut_idx],
            )
            if is_shift:
                confirmed_cut_indices.append(cut_idx)

        # 4. Group turns into Level 2 topic segments
        topic_segments: List[TopicSegmentData] = []
        start_idx = 0
        topic_counter = 1

        cut_points = sorted(list(set(confirmed_cut_indices)))
        for cut_idx in cut_points:
            seg_turns = turns[start_idx:cut_idx]
            if seg_turns:
                topic_seg = await self._build_topic_segment(topic_counter, seg_turns)
                topic_segments.append(topic_seg)
                topic_counter += 1
                start_idx = cut_idx

        # Final trailing segment
        if start_idx < len(turns):
            seg_turns = turns[start_idx:]
            topic_seg = await self._build_topic_segment(topic_counter, seg_turns)
            topic_segments.append(topic_seg)

        # 5. Compile Level 3 Executive Summary
        executive_summary = await self._compile_executive_summary(
            meeting_id=meeting_id,
            title=meeting_title,
            topics=topic_segments,
            turns=turns,
        )

        return topic_segments, executive_summary, embeddings

    async def verify_boundary(self, turn_a, turn_b, cosine_similarity=None):
        return await self._verify_boundary_with_llm(turn_a, turn_b)

    async def _verify_boundary_with_llm(
        self,
        turn_before: SpeakerTurn,
        turn_after: SpeakerTurn,
    ) -> bool:
        """
        Lightweight OpenRouter LLM validation check to confirm a true conversational shift versus a tangent.
        """
        # Tangent rule: If the next turn is a short question or < 12 words, suppress false split
        if turn_after.is_question or len(turn_after.text.split()) < 12:
            return False

        prompt = (
            f"Analyze if there is a substantive topic shift between these two meeting dialogue turns.\n"
            f"Turn A ({turn_before.speaker_name}): \"{turn_before.text}\"\n"
            f"Turn B ({turn_after.speaker_name}): \"{turn_after.text}\"\n\n"
            f"Answer ONLY 'YES' if Turn B starts a new discussion topic or agenda item.\n"
            f"Answer 'NO' if Turn B is a continuation, clarification, answer, or brief tangent."
        )

        try:
            answer = await self.llm_client.generate_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self.llm_model,
                max_tokens=5,
                temperature=self.config.llm_temperature,
            )
            return "YES" in answer.upper()
        except Exception as exc:
            logger.warning(f"OpenRouter boundary verification failed: {exc}. Using heuristic fallback.")
            words1 = set(re.findall(r"\w{4,}", turn_before.text.lower()))
            words2 = set(re.findall(r"\w{4,}", turn_after.text.lower()))
            return len(words1 & words2) <= 1

    async def _build_topic_segment(
        self,
        topic_index: int,
        segment_turns: List[SpeakerTurn],
    ) -> TopicSegmentData:
        """Build TopicSegmentData record with descriptive chapter title and compiled text block."""
        compiled_text = "\n".join([f"{t.speaker_name}: {t.text}" for t in segment_turns])
        start_time = round(min(t.start_time for t in segment_turns), 2)
        end_time = round(max(t.end_time for t in segment_turns), 2)

        # Generate chapter title using OpenRouter LLM or salient keywords
        title = await self._generate_chapter_title(segment_turns)

        return TopicSegmentData(
            topic_index=topic_index,
            title=title,
            compiled_text=compiled_text,
            start_time=start_time,
            end_time=end_time,
            turns=segment_turns,
        )

    async def _generate_chapter_title(self, segment_turns: List[SpeakerTurn]) -> str:
        """Generate an understandable, executive-level 5-to-6 word topic heading using OpenRouter."""
        sample_text = "\n".join([f"{t.speaker_name}: {t.text}" for t in segment_turns])[:1000]
        prompt = (
            "You are an executive meeting analyst. Analyze the following meeting dialogue excerpt and generate "
            "an understandable, professional 5-to-6 word topic heading that describes what was discussed.\n\n"
            "STRICT RULES:\n"
            "1. Length: Exactly 5 to 6 words.\n"
            "2. Quality: Must convey a clear, meaningful business or technical topic (e.g., "
            "'Implementing Hybrid Search And Database Caching', 'Resolving Staging Contention And Timeout Errors', "
            "'Reviewing Frontend UI Migration And Roadmap').\n"
            "3. Format: Return ONLY the title itself. Do NOT include quotes, prefixes like 'Topic:', "
            "or generic suffixes like 'Discussion' or 'Chapter'.\n"
            "4. Forbidden: NEVER use vague word lists like 'Word & Word & Word'.\n\n"
            f"Meeting Excerpt:\n{sample_text}\n\n"
            "Topic Heading:"
        )
        try:
            title = await self.llm_client.generate_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self.llm_model,
                max_tokens=30,
                temperature=self.config.llm_temperature,
            )
            cleaned_title = title.strip().strip('"').strip("'").strip()
            cleaned_title = re.sub(r"^(Topic Heading|Title|Topic|Chapter):\s*", "", cleaned_title, flags=re.IGNORECASE).strip()
            cleaned_title = cleaned_title.strip('"').strip("'").strip(".").strip()
            words = cleaned_title.split()
            if 3 <= len(words) <= 10:
                if len(words) > 6:
                    cleaned_title = " ".join(words[:6])
                return cleaned_title
        except Exception as exc:
            logger.warning(f"LLM chapter title generation failed ({exc}). Using heuristic fallback.")

        # Heuristic title fallback
        for t in segment_turns:
            if getattr(t, "is_blocker", False) or getattr(t, "is_commitment", False) or len(t.text.split()) >= 6:
                words = re.findall(r"\b[A-Za-z0-9\-_]{3,}\b", t.text)
                stopwords = {"that", "this", "with", "have", "from", "will", "they", "what", "about", "yeah", "okay", "like"}
                meaningful = [w.capitalize() for w in words if w.lower() not in stopwords]
                if len(meaningful) >= 4:
                    action = "Addressing" if getattr(t, "is_blocker", False) else ("Planning" if getattr(t, "is_commitment", False) else "Reviewing")
                    return f"{action} {' '.join(meaningful[:5])}"

        all_words = re.findall(r"\b[A-Za-z]{4,}\b", " ".join(t.text for t in segment_turns))
        stopwords = {"that", "this", "with", "have", "from", "will", "they", "what", "about", "yeah", "okay"}
        salient = [w.capitalize() for w in all_words if w.lower() not in stopwords]
        top = salient[:5] if salient else ["Discussion"]
        return f"Discussion On {' '.join(top[:4])}"

    async def _compile_executive_summary(
        self,
        meeting_id: str,
        title: Optional[str],
        topics: List[TopicSegmentData],
        turns: List[SpeakerTurn],
    ) -> str:
        """Compile a Level 3 Executive Summary summarizing the overall meeting record via OpenRouter."""
        participants = list(dict.fromkeys(t.speaker_name for t in turns if t.speaker_name))
        participants_str = ", ".join(participants) if participants else "participants"
        total_duration = round(turns[-1].end_time - turns[0].start_time, 1) if turns else 0.0

        topic_outline = "\n".join([f"- Chapter {t.topic_index} ({t.title}): {t.compiled_text[:200]}..." for t in topics])
        prompt = (
            f"Write a concise executive summary for meeting '{title or meeting_id}'.\n"
            f"Attendees: {participants_str}\n"
            f"Chapters:\n{topic_outline}\n\nExecutive Summary:"
        )
        try:
            summary_text = await self.llm_client.generate_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=self.llm_model,
                max_tokens=250,
                temperature=self.config.llm_temperature,
            )
            if summary_text and len(summary_text) > 20:
                return summary_text
        except Exception as exc:
            logger.warning(f"OpenRouter executive summary generation failed: {exc}. Falling back to template summary.")

        # Template summary fallback
        chapters_str = ", ".join([f"'{t.title}'" for t in topics])
        return (
            f"Executive Summary for meeting '{title or meeting_id}'. "
            f"The session was attended by {participants_str} spanning {total_duration}s. "
            f"Discussion was structured into {len(topics)} topic chapters: {chapters_str}."
        )


SemanticBoundaryDetector.detect_boundaries_and_topics = SemanticBoundaryDetector.detect_boundaries_and_generate_topics
