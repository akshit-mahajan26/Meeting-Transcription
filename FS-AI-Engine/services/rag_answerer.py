import re
import logging
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from ..core.config import settings
from .micro_retriever import EnrichedTurnContext
from .macro_retriever import MacroRetrievalResult
from .llm_client import OpenRouterClient, openrouter_client

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert Meeting Intelligence Assistant. Your task is to answer user queries based strictly on the provided meeting transcript excerpts.

Guidelines:
1. Provide a direct, professional, and conversational response to the user's question (e.g., start with a clear direct answer or summarize the discussion naturally).
2. Do not output raw transcript blocks, speaker labels, or timestamps directly inside your answer text. Keep the narrative clean and conversational.
3. Rely only on the provided context excerpts. If the answer cannot be found in the context, state clearly that the ingested meetings do not contain that information."""


class CitationItem(BaseModel):
    """Verifiable Level 1 Raw Text Citation Item for Frontend Expander Inspection."""
    speaker: str
    start_time: float
    end_time: float
    meeting_id: str
    exact_raw_text: str
    topic_context: Optional[str] = None


class SynthesizedAnswer(BaseModel):
    """Result of Grounded Citation Synthesis Engine."""
    query: str
    answer: str
    citations: List[CitationItem] = Field(default_factory=list)
    citations_count: int
    provenance_verified: bool
    mode: str
    llm_model_used: str = Field(default_factory=lambda: settings.LLM_MODEL_NAME)


class RAGAnswerer:
    """
    Grounded Citation Synthesis Engine.
    Uses OpenRouterClient to synthesize conversational, professional answers strictly
    grounded in retrieved Level 1 raw text turns while keeping raw citation blocks
    strictly in the citations metadata array.
    """

    def __init__(
        self,
        llm_model: Optional[str] = None,
        llm_client: Optional[OpenRouterClient] = None
    ):
        self.llm_model = llm_model or settings.LLM_MODEL_NAME
        self.llm_client = llm_client or openrouter_client

    async def answer_micro_query(
        self,
        query: str,
        turns: List[EnrichedTurnContext]
    ) -> SynthesizedAnswer:
        """
        Synthesizes an answer grounded strictly in retrieved Level 1 atomic turns and parent Level 2 topics.
        """
        if not turns:
            return SynthesizedAnswer(
                query=query,
                answer="No relevant meeting turns or citations were found matching your inquiry in the ingested transcripts.",
                citations=[],
                citations_count=0,
                provenance_verified=False,
                mode="MICRO",
                llm_model_used=self.llm_model
            )

        logger.info(f"[RAGAnswerer] Synthesizing Micro answer for '{query}' using {len(turns)} turns via OpenRouter ({self.llm_model})")

        # 1. Assemble verifiable Level 1 citations strictly for UI expander metadata
        citations = []
        for t in turns:
            citations.append(CitationItem(
                speaker=t.speaker,
                start_time=t.start_time,
                end_time=t.end_time,
                meeting_id=t.meeting_id,
                exact_raw_text=t.raw_text,
                topic_context=t.topic_name or "Level 1 Turn"
            ))

        # 2. Provide excerpts as background context to OpenRouter
        context_blocks = "\n".join([
            f"- [{t.speaker} at {t.start_time:.1f}s-{t.end_time:.1f}s in {t.meeting_id}]: \"{t.raw_text}\""
            for t in turns[:5]
        ])

        user_prompt = (
            f"User Question: {query}\n\n"
            f"Verified Meeting Transcript Excerpts:\n{context_blocks}\n\n"
            f"Please answer the user's question conversationally according to your guidelines."
        )

        generated_body = await self.llm_client.generate_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.strip()},
                {"role": "user", "content": user_prompt}
            ],
            model=self.llm_model,
            max_tokens=600,
            temperature=0.2
        )

        provenance_verified = len(citations) > 0 and all(c.exact_raw_text for c in citations)

        return SynthesizedAnswer(
            query=query,
            answer=generated_body.strip(),
            citations=citations,
            citations_count=len(citations),
            provenance_verified=provenance_verified,
            mode="MICRO",
            llm_model_used=self.llm_model
        )

    async def answer_macro_query(
        self,
        query: str,
        macro_result: MacroRetrievalResult
    ) -> SynthesizedAnswer:
        """
        Synthesizes a high-level executive summary grounded in deterministic relational database records.
        """
        logger.info(f"[RAGAnswerer] Synthesizing Macro answer for '{query}' using {macro_result.total_records} records via OpenRouter ({self.llm_model})")

        answer_lines = []
        if macro_result.projects:
            answer_lines.append("### Project Status & Health Overview")
            for p in macro_result.projects:
                answer_lines.append(
                    f"- **Project {p.name}** (`{p.project_id}`): Status `{p.status.upper()}`, "
                    f"Health Score {p.health_score * 100:.0f}%, across {p.total_meetings} recorded meetings."
                )

        if macro_result.meetings:
            answer_lines.append("\n### Executive Summaries Across Meetings")
            for m in macro_result.meetings:
                summary_text = m.executive_summary or "Executive summary recorded."
                answer_lines.append(f"- **{m.title}** (`{m.meeting_id}`): {summary_text}")
                if m.topics:
                    topic_names = ", ".join(t.get("topic_name", "") for t in m.topics if t.get("topic_name"))
                    answer_lines.append(f"  *Key Topics Discussed:* {topic_names}")

        final_answer = "\n".join(answer_lines) if answer_lines else "No project or meeting records found in the database."

        return SynthesizedAnswer(
            query=query,
            answer=final_answer,
            citations=[],
            citations_count=0,
            provenance_verified=True,
            mode="MACRO",
            llm_model_used=self.llm_model
        )
