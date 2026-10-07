import hmac
import hashlib
from fastapi import Request, HTTPException, status, Depends
from .config import get_settings, Settings


async def verify_hmac_sha256_signature(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> bytes:
    """
    FastAPI dependency to verify HMAC-SHA256 webhook signatures.
    Reads the raw body and validates it against the signature provided in headers.
    
    Supported headers:
    - X-Hub-Signature-256 (standard GitHub / webhook format, e.g. 'sha256=abcdef...')
    - X-Fireflies-Signature
    - X-Signature-SHA256
    
    Raises:
        HTTPException 401 if signature header is missing.
        HTTPException 403 if signature verification fails.
    """
    secret = settings.fireflies_webhook_secret
    if not secret:
        # If secret is intentionally not set, pass through raw body
        return await request.body()

    signature_header = (
        request.headers.get("x-hub-signature-256")
        or request.headers.get("x-fireflies-signature")
        or request.headers.get("x-signature-sha256")
        or request.headers.get("x-hub-signature")
    )

    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook signature header (e.g. X-Hub-Signature-256 or X-Fireflies-Signature)",
        )

    # Strip optional 'sha256=' prefix
    raw_signature = signature_header.strip()
    if raw_signature.startswith("sha256="):
        raw_signature = raw_signature[len("sha256="):]

    # Read raw body
    body_bytes = await request.body()

    # Calculate expected HMAC-SHA256 signature
    expected_hmac = hmac.new(
        key=secret.encode("utf-8"),
        msg=body_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison
    if not hmac.compare_digest(expected_hmac, raw_signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid HMAC-SHA256 webhook signature",
        )

    return body_bytes


def generate_hmac_sha256_signature(payload_bytes: bytes, secret: str) -> str:
    """Utility to generate HMAC-SHA256 signature for testing or simulation."""
    return "sha256=" + hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()
