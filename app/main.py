"""Example Brand Partner portal — FastAPI app.

Routes:

  GET  /                       landing page
  GET  /buy                    plan picker form
  POST /buy                    creates a MintOffice order, redirects to Stripe
  GET  /extend                 subscription picker (auto-renew sign-up)
  POST /extend                 creates a MintOffice subscription, redirects to Stripe
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
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
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
            "form-action 'self' https://checkout.stripe.com",
        )
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=15552000; includeSubDomains",
            )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Package catalog — fetched live from MintOffice, NOT hard-coded.
#
# The storefront's plan cards come from GET /api/v1/catalog: the PUBLIC
# package list (trial/starter/pro) joined with THIS partner's resolved
# pricing, in the partner's currency. Retired packages disappear and a
# price/currency change in MintOffice lands within the cache TTL — no
# portal redeploy. This is what keeps the storefront from advertising
# yesterday's packages (the old hard-coded s1/s2 problem).
#
# You (the partner) still own marketing copy: PLAN_OVERRIDES lets you
# rename a package or rewrite its blurb in your fork without touching
# MintOffice. Everything you don't override — which packages exist, the
# price, the currency — comes from MintOffice and self-updates.
# ---------------------------------------------------------------------------

# Per-slug display overrides, applied on top of the canonical catalog copy.
# Leave empty to use MintOffice's display_name/description verbatim. e.g.:
#   PLAN_OVERRIDES = {
#       "starter": {"label": "Indie", "blurb": "Perfect for side projects."},
#       "pro":     {"blurb": "Our most popular plan."},
#   }
# ``label`` overrides the whole card title; the '· <period>' suffix is
# only auto-appended when you DON'T override the label, so an override is
# verbatim.
PLAN_OVERRIDES: dict[str, dict[str, str]] = {}

# Cold-start fallback. Used ONLY when MintOffice has never been reachable
# since this process started (first request during an outage). Uses the
# CURRENT public slugs — never the retired s1/s2/s4 — so even the fallback
# can't resurrect a dead package. Prices are indicative; the real price
# always comes from the live catalog and, ultimately, from the Stripe
# Checkout session MintOffice mints.
FALLBACK_CATALOG: dict = {
    "currency": "usd",
    "packages": [
        {"tier": "trial", "display_name": "Trial",
         "description": "One day to kick the tires.",
         "featured": False, "default_credit_usd": 5,
         "durations": [{"months": 1, "label": "24 hours", "price_cents": 0}],
         "subscription": {"available": False, "price_cents": None}},
        {"tier": "starter", "display_name": "Starter",
         "description": "A month of assistant time.",
         "featured": True, "default_credit_usd": 10,
         "durations": [{"months": 1, "label": "1 month", "price_cents": 1500},
                       {"months": 3, "label": "3 months", "price_cents": 4500},
                       {"months": 12, "label": "12 months", "price_cents": 18000}],
         "subscription": {"available": True, "price_cents": 1500}},
        {"tier": "pro", "display_name": "Pro",
         "description": "Faster model, longer context.",
         "featured": False, "default_credit_usd": 0,
         "durations": [{"months": 1, "label": "1 month", "price_cents": 3900},
                       {"months": 3, "label": "3 months", "price_cents": 11700},
                       {"months": 12, "label": "12 months", "price_cents": 46800}],
         "subscription": {"available": True, "price_cents": 3900}},
    ],
}

_CURRENCY_SYMBOLS = {"usd": "$", "eur": "\u20ac", "gbp": "\u00a3"}


def _format_money(cents: int, currency: str) -> str:
    """Currency-aware whole-unit price string, e.g. '$15' or '\u20ac15'.

    Whole units only — the storefront cards are deliberately round. A
    symbol currency (usd/eur/gbp) renders as '$15'; anything else renders
    as '15 SEK'-style with the uppercased ISO code so an unmapped currency
    never silently shows a bare number.
    """
    whole = round(cents / 100)
    sym = _CURRENCY_SYMBOLS.get(currency.lower())
    return f"{sym}{whole}" if sym else f"{whole} {currency.upper()}"


# Catalog cache — same sticky-on-failure contract as the credit-options
# cache below. A transient MintOffice blip keeps the last good catalog
# instead of collapsing the storefront to the cold-start fallback.
_CATALOG_TTL_SECONDS = 300
_CATALOG_RETRY_SECONDS = 30
_catalog_cache: dict[str, object] = {"value": None, "expires_at": 0.0}


def _get_catalog() -> dict:
    """Return the partner's package catalog for rendering.

    Cache miss → fetch from MintOffice. Fetch failure → keep the last good
    catalog (sticky); if MintOffice has never been reached this process,
    use FALLBACK_CATALOG so the storefront still renders something.
    """
    now = time.time()
    cached = _catalog_cache["value"]
    expires_at = float(_catalog_cache["expires_at"])  # type: ignore[arg-type]
    if cached is not None and expires_at > now:
        return cached  # type: ignore[return-value]
    fetched = mintoffice.get_catalog()
    if fetched is not None:
        _catalog_cache["value"] = fetched
        _catalog_cache["expires_at"] = now + _CATALOG_TTL_SECONDS
        return fetched
    _catalog_cache["expires_at"] = now + _CATALOG_RETRY_SECONDS
    if cached is not None:
        return cached  # type: ignore[return-value]
    return FALLBACK_CATALOG


def _apply_overrides(slug: str, label: str, blurb: str) -> tuple[str, str]:
    ov = PLAN_OVERRIDES.get(slug) or {}
    return (ov.get("label") or label, ov.get("blurb") or blurb)


def _primary_duration(pkg: dict) -> dict:
    """The duration a one-shot /buy card bills at — the package's shortest
    offered duration (trial→24h, monthly packages→1 month)."""
    durations = pkg.get("durations") or []
    if not durations:
        return {"months": 1, "label": "", "price_cents": 0}
    return min(durations, key=lambda d: int(d.get("months") or 0) or 1)


def _order_plans() -> dict[str, dict]:
    """One-shot /buy + landing plan cards, built from the live catalog.

    One card per public package, billed at its primary duration. The label
    carries '<name> · <period>' so the landing CTA's split keeps working
    and the customer sees the duration at a glance.

    Trial note: ``duration_months`` is sent as 1 to satisfy the MintOffice
    API shape, but MintOffice special-cases trial to a fixed 24h server
    lifetime regardless of the months value.
    """
    catalog = _get_catalog()
    currency = str(catalog.get("currency") or "usd")
    plans: dict[str, dict] = {}
    for pkg in catalog.get("packages") or []:
        slug = str(pkg["tier"])
        dur = _primary_duration(pkg)
        label_base = str(pkg.get("display_name") or slug.title())
        period = str(dur.get("label") or "")
        label = f"{label_base} \u00b7 {period}" if period else label_base
        blurb = str(pkg.get("description") or "")
        label, blurb = _apply_overrides(slug, label, blurb)
        price_cents = int(dur.get("price_cents") or 0)
        plans[slug] = {
            "label": label,
            "tier": slug,
            "duration_months": int(dur.get("months") or 1),
            "price_cents": price_cents,
            "price_display": _format_money(price_cents, currency),
            "blurb": blurb,
            "featured": bool(pkg.get("featured")),
        }
    return plans


def _subscription_plans() -> dict[str, dict]:
    """/extend monthly subscription cards, built from the live catalog —
    only packages MintOffice marks subscription-capable (trial is excluded
    server-side: it's a one-day evaluation, not a recurring plan)."""
    catalog = _get_catalog()
    currency = str(catalog.get("currency") or "usd")
    plans: dict[str, dict] = {}
    for pkg in catalog.get("packages") or []:
        sub = pkg.get("subscription") or {}
        if not sub.get("available"):
            continue
        slug = str(pkg["tier"])
        label_base = str(pkg.get("display_name") or slug.title())
        label = f"{label_base} \u00b7 monthly"
        blurb = str(pkg.get("description") or "")
        label, blurb = _apply_overrides(slug, label, blurb)
        price_cents = int(sub.get("price_cents") or 0)
        plans[slug] = {
            "label": label,
            "tier": slug,
            "price_cents": price_cents,
            "price_display": _format_money(price_cents, currency),
            "blurb": blurb,
            "featured": bool(pkg.get("featured")),
        }
    return plans


# Cache of the partner's ``allowed_credit_options`` so the /buy form
# doesn't re-fetch on every page load. Two failure modes the cache has
# to handle:
#
# 1. MintOffice transient blip — we must NOT poison the cache with an
#    empty list (which would silently pin the portal in VPS-only mode
#    until the next process restart). ``get_allowed_credit_options``
#    returns ``None`` to signal "fetch failed"; we keep the last good
#    value and retry sooner.
# 2. Partner edits pricing — we want the change visible within minutes,
#    not after a deploy. Hence the TTL.
#
# Reset by the test conftest between tests; in production it lives for
# the process lifetime.
_CREDIT_OPTIONS_TTL_SECONDS = 300       # 5 min when the last fetch succeeded
_CREDIT_OPTIONS_RETRY_SECONDS = 30      # back off briefly after a failure
_credit_options_cache: dict[str, object] = {
    "value": None,        # last known good list[int] (None means never seen)
    "expires_at": 0.0,    # epoch seconds; 0 forces a fetch on next read
}


def _get_credit_options() -> list[int]:
    """Return the partner's allowed credit bundles for the /buy form.

    On a cache miss, fetch from MintOffice. On a fetch failure, keep the
    last good value (sticky) instead of falling back to an empty list —
    a one-off 502 must not pin the portal to VPS-only.
    """
    now = time.time()
    cached = _credit_options_cache["value"]
    expires_at = float(_credit_options_cache["expires_at"])  # type: ignore[arg-type]
    if cached is not None and expires_at > now:
        return list(cached)  # type: ignore[arg-type]
    fetched = mintoffice.get_allowed_credit_options()
    if fetched is not None:
        _credit_options_cache["value"] = fetched
        _credit_options_cache["expires_at"] = now + _CREDIT_OPTIONS_TTL_SECONDS
        return list(fetched)
    # Fetch failed. Keep the previous good value if we ever had one; the
    # short retry window means we re-attempt on the next page load
    # instead of every single request.
    _credit_options_cache["expires_at"] = now + _CREDIT_OPTIONS_RETRY_SECONDS
    if cached is not None:
        return list(cached)  # type: ignore[arg-type]
    return []


def _credit_usd_is_allowed(amount: int, options: list[int]) -> bool:
    """0 is always allowed (VPS-only — buy.html offers it as a separate
    radio regardless of what the partner configured). Any positive amount
    must appear in the partner's configured list."""
    if amount == 0:
        return True
    return amount in options

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
    """Liveness + configuration probe.

    ``checks.db`` is a real SQLite read+write. ``checks.mintoffice_api_key``
    is a pure config check ("is the bearer token set in .env") — it does
    NOT call MintOffice, so a MintOffice outage won't make this portal
    flap. Detecting a missing/empty key is the case that bit us in prod
    (rotation cleared .env, POST /buy started 502'ing) and is exactly
    what generic uptime monitors miss when they only watch the landing
    page or DB.

    Returns 503 when any check fails so a vanilla "GET /healthz, expect
    2xx" monitor catches both DB outages and configuration drift.
    """
    db_ok = db.healthcheck()
    api_key_state = "configured" if settings.mintoffice_api_key else "missing"
    ok = db_ok and api_key_state == "configured"
    payload = {
        "ok": ok,
        "brand": settings.partner_brand,
        "version": __version__,
        "checks": {
            "db": db_ok,
            "mintoffice_api_key": api_key_state,
        },
    }
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        status_code=200 if ok else 503,
    )


@app.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    return _render(request, "index.html", {"plans": _order_plans()})


@app.get("/buy", response_class=HTMLResponse)
def buy_form(request: Request, plan: str | None = None) -> HTMLResponse:
    """Plan picker. ``?plan=<slug>`` pre-selects a card so the landing
    page's per-plan CTA lands the customer on the right option."""
    plans = _order_plans()
    selected = plan if plan in plans else None
    return _render(request, "buy.html", {
        "plans": plans,
        "selected": selected,
        "credit_options": _get_credit_options(),
        "error": None,
    })


@app.post("/buy")
def buy_submit(
    request: Request,
    plan: str = Form(...),
    language: str = Form("en"),
    credit_usd: int = Form(0),
    idempotency_key_form: str = Form(""),
) -> Response:
    plans = _order_plans()
    spec = plans.get(plan)
    if not spec:
        raise HTTPException(status_code=422, detail=f"unknown plan: {plan}")
    if credit_usd < 0:
        credit_usd = 0
    options = _get_credit_options()
    if not _credit_usd_is_allowed(credit_usd, options):
        # The customer hand-crafted a credit_usd value outside the
        # partner's allowed bundles. Re-render the form with an error so
        # the legitimate UI keeps working but a curl bypass gets a 400.
        logger.warning(
            "Rejected /buy with disallowed credit_usd=%s (allowed=%s)",
            credit_usd, options,
        )
        return _render(request, "buy.html", {
            "plans": plans, "selected": plan,
            "credit_options": options,
            "error": "That credit bundle isn't available — please pick one of the listed options.",
        }, status_code=400)
    success_url = f"{settings.public_base_url}/thank-you?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{settings.public_base_url}/cancel"
    try:
        order = mintoffice.create_order(
            tier=spec["tier"],
            duration_months=int(spec["duration_months"]),
            credit_usd=credit_usd,
            language=language,
            success_url=success_url,
            cancel_url=cancel_url,
            product_name=f"{settings.partner_brand} {spec['label']}",
            idempotency_key=idempotency_key_form or None,
        )
    except mintoffice.MintOfficeConfigError as e:
        # MINTOFFICE_API_KEY missing from .env — the portal can't even
        # try to call MintOffice. Surface a maintenance message to the
        # customer (the truthful "could not reach" message would point
        # them at a transient retry that will never succeed). Log loud
        # so the operator notices: this won't fix itself.
        logger.error("Portal misconfigured — cannot call MintOffice: %s", e)
        return _render(request, "buy.html", {
            "plans": plans, "selected": plan,
            "credit_options": _get_credit_options(),
            "error": "Checkout is temporarily unavailable — please try again shortly.",
        }, status_code=503)
    except mintoffice.MintOfficeError as e:
        # Distinguish credential failures (401/403) from genuine API
        # rejections (422 validation, 409 conflicts, etc.). A revoked or
        # mistyped key looks like a transient failure to the customer
        # but needs an operator to fix — same maintenance message as
        # the config branch, but with a flagged log line.
        if e.status_code in (401, 403):
            logger.error(
                "MintOffice rejected our credentials (status=%s, body=%r) — "
                "rotate MINTOFFICE_API_KEY from the partner dashboard",
                e.status_code, e.body,
            )
            return _render(request, "buy.html", {
                "plans": plans, "selected": plan,
                "credit_options": _get_credit_options(),
                "error": "Checkout is temporarily unavailable — please try again shortly.",
            }, status_code=503)
        logger.warning("MintOffice rejected order: %s", e)
        return _render(request, "buy.html", {
            "plans": plans, "selected": plan,
            "credit_options": _get_credit_options(),
            "error": mintoffice.format_error(e),
        }, status_code=502)
    except (httpx.TransportError, httpx.TimeoutException) as e:
        # Pure network/transport problem — DNS, TCP, TLS, timeout. The
        # original "could not reach checkout" message is accurate here.
        logger.warning("MintOffice transport failure: %s: %s", type(e).__name__, e)
        return _render(request, "buy.html", {
            "plans": plans, "selected": plan,
            "credit_options": _get_credit_options(),
            "error": "Could not reach checkout — please try again in a minute.",
        }, status_code=502)
    except Exception:  # noqa: BLE001
        logger.exception("MintOffice call blew up")
        return _render(request, "buy.html", {
            "plans": plans, "selected": plan,
            "credit_options": _get_credit_options(),
            "error": "Could not reach checkout — please try again in a minute.",
        }, status_code=502)
    if not order.checkout_url:
        return _render(request, "buy.html", {
            "plans": plans, "selected": plan,
            "credit_options": _get_credit_options(),
            "error": "MintOffice didn't return a checkout URL.",
        }, status_code=502)
    logger.info("Order %d created for plan=%s credit=%d → %s",
                order.id, plan, credit_usd, order.checkout_url)
    return RedirectResponse(order.checkout_url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/extend", response_class=HTMLResponse)
def extend_form(
    request: Request,
    tier: str | None = None,
    email: str | None = None,
    lang: str | None = None,
) -> HTMLResponse:
    """Subscription picker for end-users coming from an agent panel.

    Query params are hints the panel can pass through:

      tier   — pre-select a card (one of s1/s2/s4)
      email  — pre-fill the email field (Stripe Customer)
      lang   — checkout locale (defaults to en)

    The page works without any of them — the user can still pick a
    plan and enter their email manually.
    """
    plans = _subscription_plans()
    selected = tier if tier in plans else None
    return _render(request, "extend.html", {
        "plans": plans,
        "selected": selected,
        "prefill_email": email or "",
        "prefill_lang": (lang or "en").strip().lower() or "en",
        "error": None,
    })


@app.post("/extend")
def extend_submit(
    request: Request,
    plan: str = Form(...),
    email: str = Form(""),
    language: str = Form("en"),
    idempotency_key_form: str = Form(""),
) -> Response:
    plans = _subscription_plans()
    spec = plans.get(plan)
    if not spec:
        raise HTTPException(status_code=422, detail=f"unknown plan: {plan}")
    email_clean = email.strip() or None
    if email_clean and "@" not in email_clean:
        return _render(request, "extend.html", {
            "plans": plans, "selected": plan,
            "prefill_email": email_clean, "prefill_lang": language,
            "error": "That doesn't look like an email address.",
        }, status_code=400)
    success_url = f"{settings.public_base_url}/thank-you?session_id={{CHECKOUT_SESSION_ID}}&kind=subscription"
    cancel_url = f"{settings.public_base_url}/cancel?kind=subscription"
    try:
        sub = mintoffice.create_subscription(
            tier=spec["tier"],
            currency="usd",
            language=language,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=email_clean,
            product_name=f"{settings.partner_brand} {spec['label']}",
            idempotency_key=idempotency_key_form or None,
        )
    except mintoffice.MintOfficeConfigError as e:
        logger.error("Portal misconfigured — cannot call MintOffice: %s", e)
        return _render(request, "extend.html", {
            "plans": plans, "selected": plan,
            "prefill_email": email_clean or "", "prefill_lang": language,
            "error": "Checkout is temporarily unavailable — please try again shortly.",
        }, status_code=503)
    except mintoffice.MintOfficeError as e:
        if e.status_code in (401, 403):
            logger.error(
                "MintOffice rejected our credentials (status=%s, body=%r) — "
                "rotate MINTOFFICE_API_KEY from the partner dashboard",
                e.status_code, e.body,
            )
            return _render(request, "extend.html", {
                "plans": plans, "selected": plan,
                "prefill_email": email_clean or "", "prefill_lang": language,
                "error": "Checkout is temporarily unavailable — please try again shortly.",
            }, status_code=503)
        logger.warning("MintOffice rejected subscription: %s", e)
        return _render(request, "extend.html", {
            "plans": plans, "selected": plan,
            "prefill_email": email_clean or "", "prefill_lang": language,
            "error": mintoffice.format_error(e),
        }, status_code=502)
    except (httpx.TransportError, httpx.TimeoutException) as e:
        logger.warning("MintOffice transport failure: %s: %s", type(e).__name__, e)
        return _render(request, "extend.html", {
            "plans": plans, "selected": plan,
            "prefill_email": email_clean or "", "prefill_lang": language,
            "error": "Could not reach checkout — please try again in a minute.",
        }, status_code=502)
    except Exception:  # noqa: BLE001
        logger.exception("MintOffice subscription call blew up")
        return _render(request, "extend.html", {
            "plans": plans, "selected": plan,
            "prefill_email": email_clean or "", "prefill_lang": language,
            "error": "Could not reach checkout — please try again in a minute.",
        }, status_code=502)
    if not sub.checkout_url:
        return _render(request, "extend.html", {
            "plans": plans, "selected": plan,
            "prefill_email": email_clean or "", "prefill_lang": language,
            "error": "MintOffice didn't return a checkout URL.",
        }, status_code=502)
    logger.info("Subscription %d created for plan=%s → %s",
                sub.id, plan, sub.checkout_url)
    return RedirectResponse(sub.checkout_url, status_code=status.HTTP_303_SEE_OTHER)


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
