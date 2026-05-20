"""Example Brand Partner portal — FastAPI app.

Routes:

  GET  /                       landing page
  GET  /buy                    plan picker form
  POST /buy                    creates a MintOffice order, redirects to Stripe
  GET  /thank-you              post-payment landing
  GET  /cancel                 abandoned-checkout landing
  POST /webhooks/mintoffice    signed inbound webhook ingest
  GET  /admin                  Basic-Auth event browser (paginated, filterable)
  GET  /healthz                liveness probe (Docker / k8s)
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import db, mintoffice, webhooks
from .config import settings

__version__ = "0.3.0"

logger = logging.getLogger("partner_portal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# When the portal is pointed at the dev MintOffice (mint.mintbot.dev) we
# render a "TEST MODE" strip at the top of every page. This is a guardrail
# for partners running rebranding work — without it, a customer can be
# served a perfectly normal-looking checkout that quietly bills via the
# dev environment.
_test_mode = ".mintbot.dev" in settings.mintoffice_api_url
templates.env.globals["test_mode"] = _test_mode
templates.env.globals["mintoffice_api_url"] = settings.mintoffice_api_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info(
        "%s portal v%s started — talking to %s",
        settings.partner_brand, __version__, settings.mintoffice_api_url,
    )
    if not settings.public_base_url.startswith("https://"):
        # MintOffice rejects non-https success_url / cancel_url before it
        # even tries Stripe — surface this at startup so the operator
        # doesn't have to debug a 422 on the first /buy attempt.
        logger.warning(
            "PUBLIC_BASE_URL=%s is not https — MintOffice will reject "
            "/orders requests built off it. Front the portal with HTTPS "
            "(Cloudflare Tunnel / Tailscale Funnel / nginx + Let's Encrypt) "
            "before testing the full checkout flow.",
            settings.public_base_url,
        )
    if not settings.mintoffice_webhook_secret:
        # Webhook URL is optional in MintOffice, so an empty secret is a
        # legitimate "I haven't wired up webhooks yet" state. But the
        # receiver path still listens, and will 401 every event until a
        # secret is configured — flag that explicitly.
        logger.warning(
            "MINTOFFICE_WEBHOOK_SECRET is empty — /webhooks/mintoffice will "
            "reject all inbound events. Set a Webhook URL on your partner "
            "row in MintOffice and paste the generated secret into .env to "
            "enable event ingest."
        )
    yield


app = FastAPI(
    title=f"{settings.partner_brand} — Brand Partner reference portal",
    version=__version__,
    docs_url=None, redoc_url=None,
    lifespan=lifespan,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defence-in-depth headers on every response.

    CSP is deliberately permissive enough for inline ``<style>`` in the
    base template plus the Google Fonts CDN — tighten further if you
    ship assets self-hosted.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=15552000; includeSubDomains",
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# Sample plans. Map the visible name → MintOffice tier + duration + the
# customer-facing price you want Stripe to charge. Edit freely; the only
# constraint is that ``tier`` is one of MintOffice's allowed tiers
# (trial, s1, s2, s4) and ``duration_days`` is one of 1, 7, 30, 90, 365.
PLANS = {
    "trial": {
        "label": "Trial · 24h",
        "tier": "trial",
        "duration_days": 1,
        "credit_usd": 0,
        "price_cents": 0,
        "blurb": "One day to kick the tires. No card-locked credit.",
        "featured": False,
    },
    "basic": {
        "label": "Basic · 30 days",
        "tier": "s1",
        "duration_days": 30,
        "credit_usd": 5,
        "price_cents": 1500,
        "blurb": "A month of assistant time. Includes $5 of usage credit.",
        "featured": False,
    },
    "pro": {
        "label": "Pro · 30 days",
        "tier": "s2",
        "duration_days": 30,
        "credit_usd": 15,
        "price_cents": 3900,
        "blurb": "Faster model, longer context. $15 of usage credit.",
        "featured": True,
    },
}

EVENT_TYPE_BADGE_CLASS = {
    "order.created": "badge-created",
    "order.paid": "badge-paid",
    "order.activated": "badge-paid",
    "agent.ready": "badge-ready",
    "agent.failed": "badge-failed",
    "order.failed": "badge-failed",
    "partner.activated": "badge-partner",
}


def _render(request: Request, template: str, ctx: dict, *, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template,
        {"brand": settings.partner_brand, **ctx},
        status_code=status_code,
    )


@app.get("/healthz")
def healthz() -> Response:
    db_ok = db.healthcheck()
    payload = {
        "ok": db_ok,
        "brand": settings.partner_brand,
        "version": __version__,
        "checks": {"db": db_ok},
    }
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        status_code=200 if db_ok else 503,
    )


@app.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    return _render(request, "index.html", {"plans": PLANS})


@app.get("/buy", response_class=HTMLResponse)
def buy_form(request: Request, plan: str | None = None) -> HTMLResponse:
    """Plan picker. ``?plan=<slug>`` pre-selects a card so the landing
    page's per-plan CTA lands the customer on the right option."""
    selected = plan if plan in PLANS else None
    return _render(request, "buy.html", {
        "plans": PLANS, "selected": selected, "error": None,
    })


@app.post("/buy")
def buy_submit(
    request: Request,
    plan: str = Form(...),
    language: str = Form("en"),
) -> Response:
    spec = PLANS.get(plan)
    if not spec:
        return _render(request, "buy.html", {
            "plans": PLANS, "selected": None,
            "error": "Pick a valid plan, please.",
        }, status_code=400)
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
        return _render(request, "buy.html", {
            "plans": PLANS, "selected": plan,
            "error": mintoffice.format_error(e),
        }, status_code=502)
    except Exception:  # noqa: BLE001
        logger.exception("MintOffice call blew up")
        return _render(request, "buy.html", {
            "plans": PLANS, "selected": plan,
            "error": "Could not reach checkout — please try again in a minute.",
        }, status_code=502)
    if not order.checkout_url:
        return _render(request, "buy.html", {
            "plans": PLANS, "selected": plan,
            "error": "MintOffice didn't return a checkout URL.",
        }, status_code=502)
    logger.info("Order %d created for plan=%s → %s", order.id, plan, order.checkout_url)
    return RedirectResponse(order.checkout_url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/thank-you", response_class=HTMLResponse)
def thank_you(request: Request) -> HTMLResponse:
    return _render(request, "thank_you.html", {})


@app.get("/cancel", response_class=HTMLResponse)
def cancelled(request: Request) -> HTMLResponse:
    return _render(request, "cancel.html", {})


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
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="non-utf-8 body")
    inserted = db.store_event(
        event_id=x_mintbot_event_id,
        event_type=x_mintbot_event_type,
        payload=body_text,
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
    type: str | None = Query(default=None, max_length=64),
    offset: int = Query(default=0, ge=0, le=100_000),
) -> HTMLResponse:
    _require_admin(authorization)
    limit = settings.admin_page_size
    event_type = type or None
    total = db.count_events(event_type=event_type)
    events = db.list_events(limit=limit, offset=offset, event_type=event_type)
    known_types = db.known_event_types()
    rendered = []
    for ev in events:
        try:
            pretty = json.dumps(json.loads(ev["payload"]), indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            pretty = ev["payload"]
        rendered.append({
            **ev,
            "pretty": pretty,
            "payload_size": len(ev["payload"].encode("utf-8")),
            "badge_class": EVENT_TYPE_BADGE_CLASS.get(ev["event_type"], ""),
        })
    return _render(request, "admin.html", {
        "events": rendered,
        "total_events": total,
        "event_type_filter": event_type,
        "known_types": known_types,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
    })


# Branded HTML error pages — friendlier than FastAPI's default JSON.

def _error_page(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept and "*/*" not in accept:
        # API/curl clients keep the JSON they asked for.
        return Response(
            content=json.dumps({"error": title, "detail": message}),
            media_type="application/json",
            status_code=status_code,
        )
    return _render(request, "error.html", {
        "status_code": status_code, "title": title, "message": message,
    }, status_code=status_code)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return _error_page(
        request, 404, "Page not found",
        "We looked everywhere — that page doesn't exist.",
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return _error_page(
        request, 500, "Something went wrong",
        "An unexpected error occurred. Please try again in a moment.",
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return _error_page(request, 422, "Invalid request", str(exc.errors()[:3]))
