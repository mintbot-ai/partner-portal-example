"""Thin httpx client for the MintOffice Partner API.

Only the endpoints this portal actually exercises are wrapped — keep it
intentionally narrow. Add more as you need them. Full API reference:
the public mintbot docs at https://mintbot.how/partner-api/.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx

from .config import settings


class MintOfficeError(RuntimeError):
    """Raised on non-2xx responses. ``body`` is the parsed error payload
    (``{"error": {...}, "request_id": "..."}``) when MintOffice could
    decode the request, or ``None`` when transport failed entirely."""

    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self.body = body or {}
        super().__init__(f"MintOffice API error {status_code}: {body!r}")


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
        timeout=15.0,
        headers={
            "Authorization": f"Bearer {settings.mintoffice_api_key}",
            "User-Agent": "partner-portal-example/0.1",
        },
    )


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
) -> CreatedOrder:
    """POST /api/v1/orders.

    ``product_name`` becomes the Stripe Checkout line-item title shown to
    the customer — leave it None and MintOffice falls back to a generic
    ``S2 · 30d``-style label. Pass something branded like
    ``"AcmeAI Assistant · 30 days"`` for a white-label checkout page.
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
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    with _client() as c:
        r = c.post("/orders", json=body, headers=headers)
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if r.status_code >= 400:
        raise MintOfficeError(r.status_code, payload)
    return CreatedOrder(
        id=int(payload["id"]),
        status=str(payload["status"]),
        checkout_url=str(payload.get("checkout_url") or ""),
        amount_cents=int(payload.get("amount_cents") or 0),
        currency=str(payload.get("currency") or "usd"),
    )
