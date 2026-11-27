"""The exact cause of a failed ingestion: one place for the wording shown on the Ingest page and for whether a
failure is retried automatically. `meetings.failure_kind` stores the key; `meetings.summary` stores the reason text.

Reason = what went wrong (one short sentence). Next step = what the user should do. Permanent causes are never
retried automatically (waiting cannot fix them); the user deletes the meeting or fixes the cause and presses Retry.
"""

from __future__ import annotations

from dataclasses import dataclass

AUTO_RETRY_STEP = "None: retried automatically."


@dataclass(frozen=True)
class FailureKind:
    key: str
    reason: str
    next_step: str
    retryable: bool
    status: str  # Status column text when not retried automatically (retryable kinds show "Retrying until {date}")


_KINDS = (
    FailureKind("no_transcript", "The Fireflies meeting has no transcript (nothing was recorded or transcribed).",
                "Delete it. Retry only if a transcript appears in Fireflies later.", False, "No transcript"),
    FailureKind("not_found", "No transcript with this ID exists in Fireflies.",
                "Check the link, or delete it.", False, "Not found in Fireflies"),
    FailureKind("no_access", "This Fireflies account has no access to this transcript.",
                "Delete it, or reconnect with an account that has access.", False, "No access"),
    FailureKind("key_rejected", "Fireflies rejected the API key.",
                "Reconnect Fireflies with a valid key, then Retry.", False, "Needs action"),
    FailureKind("disconnected", "Fireflies is not connected.",
                "Connect Fireflies, then Retry.", False, "Needs action"),
    FailureKind("key_unreadable", "The stored Fireflies key can no longer be decrypted.",
                "Reconnect Fireflies, then Retry.", False, "Needs action"),
    FailureKind("fireflies_account", "Fireflies refused the request.",  # the Fireflies message is appended when given
                "Fix the issue in Fireflies, then Retry.", False, "Needs action"),
    FailureKind("fireflies_invalid_request", "Fireflies rejected the request as invalid.",
                "Delete it, or contact support.", False, "Cannot be processed"),
    FailureKind("engine_rejected", "The AI engine could not process this transcript's content.",
                "Delete it, or contact support.", False, "Cannot be processed"),
    FailureKind("rate_limited", "Fireflies rate limit reached.", AUTO_RETRY_STEP, True, "Failed"),
    FailureKind("fireflies_unavailable", "Fireflies could not be reached.", AUTO_RETRY_STEP, True, "Failed"),
    FailureKind("engine_unavailable", "The AI engine could not be reached.", AUTO_RETRY_STEP, True, "Failed"),
    FailureKind("timeout", "Processing exceeded the time limit.", AUTO_RETRY_STEP, True, "Failed"),
    FailureKind("embedding_failed", "Creating search embeddings failed at the AI provider.", AUTO_RETRY_STEP, True, "Failed"),
    FailureKind("database_error", "Saving the meeting to the database failed.", AUTO_RETRY_STEP, True, "Failed"),
    FailureKind("unknown", "Unexpected error.",  # the error type is appended: "Unexpected error (ValueError)."
                "None: retried automatically; contact support if it keeps failing.", True, "Failed"),
)
KINDS: dict[str, FailureKind] = {k.key: k for k in _KINDS}

RETRIES_STOPPED_STEP = "Press Retry, or delete it."


def kind(key: str | None) -> FailureKind | None:
    return KINDS.get(key or "")


def reason_text(key: str, detail: str | None = None) -> str:
    """The Reason column text for a cause; `detail` is Fireflies' own message or the error type, when relevant."""
    k = KINDS[key]
    if key == "fireflies_account" and detail:
        return f"Fireflies refused the request: {detail.rstrip('.')}."
    if key == "unknown" and detail:
        return f"Unexpected error ({detail})."
    return k.reason


def next_step(key: str | None, retries_open: bool) -> str | None:
    """The Next step column text. A retryable cause whose 7-day window has closed asks the user to act."""
    k = kind(key)
    if k is None:
        return None
    if k.retryable and not retries_open:
        return RETRIES_STOPPED_STEP
    return k.next_step


def status_label(key: str | None, retries_open: bool, retry_until_text: str | None) -> str:
    """The Status column text for a FAILED meeting."""
    k = kind(key)
    if k is not None and not k.retryable:
        return k.status
    if retries_open and retry_until_text:
        return f"Failed, retrying until {retry_until_text}"
    return "Failed, retries stopped"
