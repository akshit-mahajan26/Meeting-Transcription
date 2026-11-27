"""Background ingestion jobs, run in-process by app/services/ingest_runner.py."""

import asyncio
import logging

from ..core.config import get_settings
from .fireflies_client import get_fireflies_client
from .ingestion_failures import record_failure
from .pipeline_orchestrator import get_backend_pipeline_orchestrator

logger = logging.getLogger(__name__)

async def run_fireflies_ingest(user_id: str, transcript_id: str, project_id: str | None, title: str | None) -> None:
    """Fetches a Fireflies transcript and ingests it. Never raises: every failure is recorded on the meeting."""
    logger.info(f"[Ingest Job] Initiating transcript fetch for ID: '{transcript_id}'...")
    try:
        transcript_data = await get_fireflies_client().fetch_transcript_by_id(
            transcript_id=transcript_id,
            save_to_file=get_settings().save_raw_transcripts,
            user_id=user_id,
        )
        logger.info(f"[Ingest Job] Fetched transcript for '{transcript_id}'. Running AI Engine pipeline...")
        result = await get_backend_pipeline_orchestrator().orchestrate_meeting_ingestion(
            transcript_dict=transcript_data,
            user_id=user_id,
            project_id=project_id,
            custom_title=title,
        )
        logger.info(
            f"[Ingest Job] Finished meeting '{result.get('meeting_id')}': status={result.get('status')}, "
            f"is_idempotent_hit={result.get('is_idempotent_hit')}, total_turns={result.get('total_turns')}, "
            f"total_topics={result.get('total_topics')}"
        )
    except Exception as exc:
        logger.error(f"[Ingest Job] Failed to process transcript '{transcript_id}': {exc}", exc_info=True)
        await asyncio.to_thread(record_failure, user_id, transcript_id, project_id, title, exc)


async def run_raw_media_upload(media_url: str, title: str | None) -> None:
    """Submits an external recording to Fireflies for transcription. Never raises."""
    logger.info("[Ingest Job] Submitting media file URL to Fireflies...")
    try:
        result = await get_fireflies_client().upload_audio_from_url(audio_url=media_url, title=title)
        logger.info(f"[Ingest Job] Audio upload completed: {result}")
    except Exception as exc:
        logger.error(f"[Ingest Job] Failed to upload audio file '{media_url}': {exc}", exc_info=True)
