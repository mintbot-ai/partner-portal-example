"""Example Brand Partner portal — FastAPI app.

Routes:

  GET  /                       landing page
  GET  /buy                    plan picker form
  POST /buy                    creates a MintOffice order, redirects to Stripe
  GET  /thank-you              post-payment landing
  GET  /cancel                 abandoned-checkout landing
  POST /webhooks/mintoffice    signed inbound webhook ingest
  GET  /admin                  Basic-Auth event browser
  GET  /healthz                liveness probe (Docker / k8s)
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, mintoffice, webhooks
from .config import settings

logger = logging.getLogger("partner_portal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info(
        "%s portal started — talking to %s",
        settings.partner_brand, settings.mintoffice_api_url,
    )
    yield


app = FastAPI(
    title=f"{settings.partner_brand} — Brand Partner reference portal",
    docs_url=None, redoc_url=None,
    lifespan=lifespan,
)


# Hardcoded sample plans. Map the visible name → MintOffice tier + duration.
# Edit freely; the only constraint is that ``tier`` is one of MintOffice's
# allowed tiers (trial, s1, s2, s4) and ``duration_days`` is one of
# 1, 7, 30, 90, 365.
PLANS = {
    "trial": {
        "label": "Trial · 24h",
        "tier": "trial",
        "duration_days": 1,
        "credit_usd": 0,
        "blurb": "One day to kick the tires. No card-locked credit.",
    },
    "basic": {
        "label": "Basic · 30 days",
        "tier": "s1",
        "duration_days": 30,
        "credit_usd": 5,
        "blurb": "A month of AcmeAI Assistant. Includes $5 of usage credit.",
    },
    "pro": {
        "label": "Pro · 30 days",
        "tier": "s2",
        "duration_days": 30,
        "credit_usd": 15,
        "blurb": "Faster model, longer context. $15 of usage credit.",
    },
}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "brand": settings.partner_brand}


@app.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html",
        {"brand": settings.partner_brand, "plans": PLANS},
    )


@app.get("/buy", response_class=HTMLResponse)
def buy_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "buy.html",
        {"brand": settings.partner_brand, "plans": PLANS, "error": None},
    )


@app.post("/buy")
def buy_submit(
    request: Request,
    plan: str = Form(...),
    language: str = Form("en"),
) -> Response:
    spec = PLANS.get(plan)
    if not spec:
        return templates.TemplateResponse(
            request, "buy.html",
            {"brand": settings.partner_brand, "plans": PLANS,
             "error": "Pick a valid plan, please."},
            status_code=400,
        )
    success_url = f"{settings.public_base_url}/thank-you?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{settings.public_base_url}/cancel"
    try:
        order = mintoffice.create_order(
            tier=spec["tier"],
            duration_days=int(spec["duration_days"]),
            credit_usd=int(spec["credit_usd"]),
            language=language,
            success_url=success_url,
            cancel_url=cancel_url,
            product_name=f"{settings.partner_brand} {spec['label']}",
        )
    except mintoffice.MintOfficeError as e:
        logger.warning("MintOffice rejected order: %s", e)
        msg = (
            (e.body.get("error") or {}).get("message")
            if isinstance(e.body, dict) else None
        ) or f"MintOffice returned {e.status_code}."
        return templates.TemplateResponse(
            request, "buy.html",
            {"brand": settings.partner_brand, "plans": PLANS, "error": msg},
            status_code=502,
        )
    except Exception:  # noqa: BLE001
        logger.exception("MintOffice call blew up")
        return templates.TemplateResponse(
            request, "buy.html",
            {"brand": settings.partner_brand, "plans": PLANS,
             "error": "Could not reach checkout — please try again in a minute."},
            status_code=502,
        )
    if not order.checkout_url:
        return templates.TemplateResponse(
            request, "buy.html",
            {"brand": settings.partner_brand, "plans": PLANS,
             "error": "MintOffice didn't return a checkout URL."},
            status_code=502,
        )
    logger.info("Order %d created for plan=%s → %s", order.id, plan, order.checkout_url)
    return RedirectResponse(order.checkout_url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/thank-you", response_class=HTMLResponse)
def thank_you(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "thank_you.html",
        {"brand": settings.partner_brand},
    )


@app.get("/cancel", response_class=HTMLResponse)
def cancelled(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "cancel.html",
        {"brand": settings.partner_brand},
    )


@app.post("/webhooks/mintoffice")
async def receive_webhook(
    request: Request,
    x_mintbot_signature: str | None = Header(default=None, alias="X-Mintbot-Signature"),
    x_mintbot_event_id: str | None = Header(default=None, alias="X-Mintbot-Event-Id"),
    x_mintbot_event_type: str | None = Header(default=None, alias="X-Mintbot-Event-Type"),
) -> Response:
    raw = await request.body()
    verdict = webhooks.verify_signature(
        settings.mintoffice_webhook_secret, raw, x_mintbot_signature,
    )
    if not verdict.ok:
        logger.warning(
            "Webhook rejected: %s id=%s type=%s",
            verdict.reason, x_mintbot_event_id, x_mintbot_event_type,
        )
        raise HTTPException(status_code=401, detail="invalid signature")
    if not x_mintbot_event_id or not x_mintbot_event_type:
        raise HTTPException(status_code=400, detail="missing event headers")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="non-utf-8 body")
    inserted = db.store_event(
        event_id=x_mintbot_event_id,
        event_type=x_mintbot_event_type,
        payload=raw.decode("utf-8"),
    )
    logger.info(
        "Webhook %s id=%s — %s",
        x_mintbot_event_type, x_mintbot_event_id,
        "stored" if inserted else "duplicate (idempotent)",
    )
    return Response(status_code=200, content='{"ok":true}', media_type="application/json")


def _require_admin(authorization: str | None) -> None:
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="admin password not configured")
    if not authorization or not authorization.lower().startswith("basic "):
        raise HTTPException(
            status_code=401, detail="auth required",
            headers={"WWW-Authenticate": 'Basic realm="admin"'},
        )
    try:
        decoded = base64.b64decode(authorization[6:].strip()).decode("utf-8")
        user, _, pwd = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="malformed auth",
                            headers={"WWW-Authenticate": 'Basic realm="admin"'})
    if not (
        secrets.compare_digest(user, settings.admin_username)
        and secrets.compare_digest(pwd, settings.admin_password)
    ):
        raise HTTPException(status_code=401, detail="bad credentials",
                            headers={"WWW-Authenticate": 'Basic realm="admin"'})


@app.get("/admin", response_class=HTMLResponse)
def admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> HTMLResponse:
    _require_admin(authorization)
    events = db.list_events(limit=200)
    # Prettify the JSON payload for display only — original raw bytes
    # are already stored verbatim in SQLite.
    rendered = []
    for ev in events:
        try:
            pretty = json.dumps(json.loads(ev["payload"]), indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            pretty = ev["payload"]
        rendered.append({**ev, "pretty": pretty})
    return templates.TemplateResponse(
        request, "admin.html",
        {"brand": settings.partner_brand, "events": rendered},
    )
