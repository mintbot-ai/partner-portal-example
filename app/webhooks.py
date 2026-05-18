"""Inbound webhook signature verification.

MintOffice signs every webhook with HMAC-SHA256 over ``f"{ts}.{body}"``
where ``body`` is the raw JSON bytes posted. The header layout mirrors
Stripe's so partners can reuse most of an existing helper:

    X-Mintbot-Signature: t=<unix_ts>,v1=<hex>

We accept a 5-minute clock skew window by default — anything older is
rejected as a likely replay attempt.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


DEFAULT_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class SignatureVerdict:
    ok: bool
    reason: str = ""


def verify_signature(
    secret: str,
    raw_body: bytes,
    header_value: str | None,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: int | None = None,
) -> SignatureVerdict:
    """Verify a ``t=<int>,v1=<hex>`` signature against the raw request body.

    Returns a verdict object so callers can log the reason without
    leaking it to the wire (we always return 401 on the HTTP side
    regardless of which check failed).
    """
    if not secret:
        return SignatureVerdict(False, "no_secret_configured")
    if not header_value:
        return SignatureVerdict(False, "missing_header")
    try:
        ts_part, v1_part = header_value.split(",", 1)
        ts = int(ts_part.split("=", 1)[1])
        sig = v1_part.split("=", 1)[1].strip()
    except (ValueError, IndexError):
        return SignatureVerdict(False, "malformed_header")
    cur = int(time.time()) if now is None else int(now)
    if abs(cur - ts) > tolerance_seconds:
        return SignatureVerdict(False, "stale_timestamp")
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return SignatureVerdict(False, "bad_signature")
    return SignatureVerdict(True)
