"""Background ingestion jobs, run in-process by app/services/ingest_runner.py."""

import asyncio
import logging

from ..core.config import get_settings
from ..core.crypto import SecretDecryptionError, decrypt_secret
from ..core.database import SessionLocal, UserModel
from .fireflies_client import FirefliesAPIError, FirefliesClient, classify_fireflies_error, get_fireflies_client
from .ingestion_failures import FirefliesDisconnected, record_failure
from .pipeline_orchestrator import get_backend_pipeline_orchestrator

logger = logging.getLogger(__name__)

def _owner_key(user_id: str) -> str | None:
    """The owner's decrypted linked Fireflies key, or None when not linked. Raises SecretDecryptionError."""
    db = SessionLocal()
    try:
        token = db.query(UserModel.fireflies_api_key_encrypted).filter(UserModel.id == user_id).scalar()
    finally:
        db.close()
    return decrypt_secret(token) if token else None


async def run_fireflies_ingest(
    user_id: str, transcript_id: str, project_id: str | None, title: str | None, source: str = "link"
) -> None:
    """Fetches a Fireflies transcript and ingests it. Never raises: every failure is recorded on the meeting."""
    logger.info(f"[Ingest Job] Initiating transcript fetch for ID: '{transcript_id}'...")
    try:
        try:
            api_key = await asyncio.to_thread(_owner_key, user_id)
        except SecretDecryptionError as exc:
            raise FirefliesDisconnected("The stored Fireflies key can no longer be read. Reconnect Fireflies.") from exc
        if api_key is None and source == "fireflies_sync":
            raise FirefliesDisconnected("Fireflies is disconnected. Reconnect to retry this meeting.")
        client = FirefliesClient(api_key=api_key) if api_key else get_fireflies_client()
        is_link = source != "fireflies_sync"
        try:
            transcript_data = await client.fetch_transcript_by_id(
                transcript_id=transcript_id,
                save_to_file=get_settings().save_raw_transcripts if is_link else False,
                user_id=user_id,
            )
        except FirefliesAPIError as fetch_exc:
            if classify_fireflies_error(fetch_exc) != "no_access":
                raise
            try:
                await client.get_current_user()
            except Exception:
                raise fetch_exc from None
            # The key works, so this account simply cannot see the transcript: skipped, not failed.
            logger.error(f"[Ingest Job] Transcript '{transcript_id}' is not accessible to this account: {fetch_exc}")
            await asyncio.to_thread(record_failure, user_id, transcript_id, project_id, title, fetch_exc, source, True)
            return
        logger.info(f"[Ingest Job] Fetched transcript for '{transcript_id}'. Running AI Engine pipeline...")
        result = await get_backend_pipeline_orchestrator().orchestrate_meeting_ingestion(
            transcript_dict=transcript_data,
            user_id=user_id,
            project_id=project_id,
            custom_title=title if is_link else None,
            source=source,
        )
        logger.info(
            f"[Ingest Job] Finished meeting '{result.get('meeting_id')}': status={result.get('status')}, "
            f"is_idempotent_hit={result.get('is_idempotent_hit')}, total_turns={result.get('total_turns')}, "
            f"total_topics={result.get('total_topics')}"
        )
    except Exception as exc:
        logger.error(f"[Ingest Job] Failed to process transcript '{transcript_id}': {exc}", exc_info=True)
        await asyncio.to_thread(record_failure, user_id, transcript_id, project_id, title, exc, source)


async def run_raw_media_upload(media_url: str, title: str | None) -> None:
    """Submits an external recording to Fireflies for transcription. Never raises."""
    logger.info("[Ingest Job] Submitting media file URL to Fireflies...")
    try:
        result = await get_fireflies_client().upload_audio_from_url(audio_url=media_url, title=title)
        logger.info(f"[Ingest Job] Audio upload completed: {result}")
    except Exception as exc:
        logger.error(f"[Ingest Job] Failed to upload audio file '{media_url}': {exc}", exc_info=True)
