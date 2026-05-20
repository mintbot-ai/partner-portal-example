"""Thin httpx client for the MintOffice Partner API.

Only the endpoints this portal actually exercises are wrapped — keep it
intentionally narrow. Add more as you need them. Full API reference:
the public mintbot docs at https://mintbot.how/partner-api/.

Retries: idempotent POSTs to ``/orders`` (an ``Idempotency-Key`` makes
them safe) retry on 5xx and transport errors. The MintOffice API is
intentionally idempotent on that key, so MintOffice will collapse
duplicate POSTs server-side.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import httpx

from .config import settings

logger = logging.getLogger("partner_portal.mintoffice")

USER_AGENT = "partner-portal-example/0.3"
RETRYABLE_STATUS = frozenset({502, 503, 504})


class MintOfficeError(RuntimeError):
    """Raised on non-2xx responses. ``body`` is the parsed error payload
    (``{"error": {...}, "request_id": "..."}``) when MintOffice could
    decode the request, or ``None`` when transport failed entirely."""

    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self.body = body or {}
        super().__init__(f"MintOffice API error {status_code}: {body!r}")


def format_error(e: "MintOfficeError") -> str:
    """Best-effort human-readable explanation for a MintOfficeError.

    MintOffice currently emits two response shapes for failures:

    - Business errors: ``{"error": {"code": "...", "message": "..."}}``
    - FastAPI validation (422): ``{"detail": [{"loc": [...], "msg": "..."}]}``

    Surface whichever applies; fall back to ``MintOffice returned <code>.``
    """
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    detail = body.get("detail")
    if isinstance(detail, list) and detail:
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            msg = item.get("msg") or ""
            loc = item.get("loc") or []
            field = loc[-1] if loc else None
            if field and msg:
                parts.append(f"{field}: {msg}")
            elif msg:
                parts.append(str(msg))
        if parts:
            return " · ".join(parts)
    if isinstance(detail, str) and detail:
        return detail
    return f"MintOffice returned {e.status_code}."


@dataclass(frozen=True)
class CreatedOrder:
    id: int
    status: str
    checkout_url: str
    amount_cents: int
    currency: str


def _client() -> httpx.Client:
    if not settings.mintoffice_api_key:
        raise RuntimeError(
            "MINTOFFICE_API_KEY is empty — set it in .env before calling MintOffice"
        )
    return httpx.Client(
        base_url=f"{settings.mintoffice_api_url}/api/v1",
        timeout=settings.mintoffice_timeout_seconds,
        headers={
            "Authorization": f"Bearer {settings.mintoffice_api_key}",
            "User-Agent": USER_AGENT,
        },
    )


def _post_with_retry(client: httpx.Client, path: str, *, json: dict, headers: dict) -> httpx.Response:
    """POST with capped exponential backoff on 5xx / network errors.

    The Idempotency-Key in ``headers`` makes retries safe — MintOffice
    collapses duplicate POSTs server-side.
    """
    attempts = settings.mintoffice_retries + 1
    backoff = 0.5
    last_exc: Exception | None = None
    for n in range(1, attempts + 1):
        try:
            r = client.post(path, json=json, headers=headers)
            if r.status_code in RETRYABLE_STATUS and n < attempts:
                logger.warning(
                    "MintOffice %s returned %s (attempt %d/%d) — retrying in %.1fs",
                    path, r.status_code, n, attempts, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            return r
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if n >= attempts:
                break
            logger.warning(
                "MintOffice %s transport error %s (attempt %d/%d) — retrying in %.1fs",
                path, type(exc).__name__, n, attempts, backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 4.0)
    assert last_exc is not None  # only path that exits the loop without return
    raise last_exc


def create_order(
    *,
    tier: str,
    duration_days: int,
    credit_usd: int,
    language: str,
    success_url: str,
    cancel_url: str,
    product_name: str | None = None,
    external_id: str | None = None,
    idempotency_key: str | None = None,
) -> CreatedOrder:
    """POST /api/v1/orders.

    ``product_name`` becomes the Stripe Checkout line-item title shown to
    the customer — leave it None and MintOffice falls back to a generic
    ``S2 · 30d``-style label. Pass something branded like
    ``"AcmeAI Assistant · 30 days"`` for a white-label checkout page.

    Pass ``idempotency_key`` when the caller wants to make a particular
    submit idempotent across the network boundary (e.g. a hidden form
    field that survives an accidental refresh). When unset, a fresh uuid4
    is generated per call.
    """
    body: dict = {
        "tier": tier,
        "duration_days": duration_days,
        "credit_usd": credit_usd,
        "language": language,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    if product_name:
        body["product_name"] = product_name
    if external_id:
        body["external_id"] = external_id
    headers = {"Idempotency-Key": idempotency_key or str(uuid.uuid4())}
    with _client() as c:
        r = _post_with_retry(c, "/orders", json=body, headers=headers)
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if r.status_code >= 400:
        raise MintOfficeError(r.status_code, payload)
    payload = payload or {}
    return CreatedOrder(
        id=int(payload.get("id") or 0),
        status=str(payload.get("status") or ""),
        checkout_url=str(payload.get("checkout_url") or ""),
        amount_cents=int(payload.get("amount_cents") or 0),
        currency=str(payload.get("currency") or "usd"),
    )


def get_allowed_credit_options() -> list[int]:
    """GET /api/v1/settings → list of credit bundle sizes the partner allows.

    MintOffice stores the partner's chosen subset of ``(5, 10, 20, 50)``
    USD as ``allowed_credit_options`` on the pricing section. An empty
    list means the partner only sells the VPS (clients bring their own
    Codex / Claude API key).

    Strictly best-effort: any failure (missing API key, transport error,
    non-2xx, malformed body) returns an empty list so the /buy page
    degrades gracefully to VPS-only instead of 500ing.
    """
    try:
        with _client() as c:
            r = c.get("/settings")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not reach MintOffice /settings: %s", exc)
        return []
    if r.status_code >= 400:
        logger.warning("MintOffice /settings returned %s", r.status_code)
        return []
    try:
        payload = r.json()
    except ValueError:
        return []
    raw = payload.get("allowed_credit_options") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    return out
