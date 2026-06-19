"""Tests for the /buy one-shot order flow.

The storefront's plan cards now come from GET /api/v1/catalog (the live,
partner-scoped package list) instead of a hard-coded PLANS dict — so every
test that renders /buy or posts to it registers a catalog response. The
public package slugs are trial/starter/pro; the retired s1/s2/s4 slugs are
gone and must never reappear.
"""
import sys

import pytest
from unittest.mock import patch


# A representative live catalog as MintOffice's GET /api/v1/catalog returns
# it: public packages only (trial/starter/pro), partner-resolved prices in
# the partner's currency. pytest-httpx reuses a registered response for
# every matching request, so one registration covers repeated fetches.
CATALOG = {
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


def _add_catalog(httpx_mock, catalog=None):
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/catalog",
        json=catalog if catalog is not None else CATALOG,
        status_code=200,
    )


def test_buy_shows_credit_options_from_partner_settings(client, httpx_mock):
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20]},
        status_code=200,
    )
    resp = client.get("/buy")
    assert resp.status_code == 200
    assert 'value="10"' in resp.text
    assert 'value="20"' in resp.text
    assert 'value="0"' in resp.text  # VPS-only option always present


def test_buy_lists_public_packages_only(client, httpx_mock):
    """Regression — the storefront must render the live catalog's public
    packages (trial/starter/pro) and never the retired s1/s2/s4 slugs."""
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10]},
        status_code=200,
    )
    resp = client.get("/buy")
    assert resp.status_code == 200
    assert 'name="plan" value="starter"' in resp.text
    assert 'name="plan" value="pro"' in resp.text
    assert 'name="plan" value="trial"' in resp.text
    for dead in ("s1", "s2", "s4"):
        assert f'name="plan" value="{dead}"' not in resp.text


def test_buy_no_credit_options_shows_hidden_input(client, httpx_mock):
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": []},
        status_code=200,
    )
    resp = client.get("/buy")
    assert resp.status_code == 200
    assert 'type="hidden"' in resp.text
    assert 'name="credit_usd"' in resp.text


def test_buy_pre_selects_plan_from_query_string(client, httpx_mock):
    """Regression — the credit radios are *also* `checked` by default
    (first option), so verify the plan radio specifically is the selected
    one and no other plan radio carries checked."""
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20, 50]},
        status_code=200,
    )
    resp = client.get("/buy?plan=starter")
    assert resp.status_code == 200
    assert 'name="plan" value="starter" required' in resp.text  # sanity
    idx = resp.text.find('name="plan" value="starter"')
    snippet = resp.text[idx : idx + 200]
    assert " checked" in snippet, snippet
    for other in ("trial", "pro"):
        oidx = resp.text.find(f'name="plan" value="{other}"')
        assert oidx >= 0
        assert " checked" not in resp.text[oidx : oidx + 200]


def test_buy_post_with_credit_usd(client, httpx_mock):
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10]},
        status_code=200,
    )
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/orders",
        json={"checkout_url": "https://stripe.test/pay"}, status_code=200,
    )
    resp = client.post("/buy", data={"plan": "trial", "credit_usd": "10", "idempotency_key_form": "key123"}, follow_redirects=False)
    assert resp.status_code == 303


def test_buy_post_sends_duration_months_to_mintoffice(client, httpx_mock):
    """Regression — the Brand Partner API takes ``duration_months`` (1/3/12),
    not ``duration_days``. Don't loosen the assertion."""
    import json as _json
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10]},
        status_code=200,
    )
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/orders",
        json={"checkout_url": "https://stripe.test/pay"},
        status_code=200,
    )
    resp = client.post(
        "/buy",
        data={"plan": "starter", "credit_usd": "10"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    order_calls = [
        r for r in httpx_mock.get_requests()
        if r.url.path.endswith("/orders") and r.method == "POST"
    ]
    assert len(order_calls) == 1
    body = _json.loads(order_calls[0].content.decode())
    assert "duration_months" in body, body
    assert body["duration_months"] == 1
    assert body["tier"] == "starter"
    assert "duration_days" not in body, body


def test_buy_post_unknown_plan(client, httpx_mock):
    _add_catalog(httpx_mock)
    resp = client.post("/buy", data={"plan": "unknown", "credit_usd": "0"})
    assert resp.status_code == 422


def test_buy_post_rejects_disallowed_credit_usd(client, httpx_mock):
    """Server-side allow-list — a hand-crafted POST with credit_usd=50
    when the partner only allows [10, 20] must 400, not pass through."""
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20]},
        status_code=200,
    )
    resp = client.post(
        "/buy",
        data={"plan": "starter", "credit_usd": "50"},
        follow_redirects=False,
    )
    assert resp.status_code == 400, resp.text
    assert "isn't available" in resp.text or "isn&#39;t available" in resp.text


def test_buy_post_accepts_zero_credit_even_when_options_set(client, httpx_mock):
    """0 (VPS-only) is always allowed regardless of the partner's list."""
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20]},
        status_code=200,
    )
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/orders",
        json={"checkout_url": "https://stripe.test/pay"},
        status_code=200,
    )
    resp = client.post(
        "/buy",
        data={"plan": "starter", "credit_usd": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_buy_post_missing_api_key_shows_maintenance(client, monkeypatch, caplog):
    """Regression — empty MINTOFFICE_API_KEY must surface a maintenance
    message (503) and a loud ERROR log, not the misleading transport
    message. With no key the catalog fetch fails too and the storefront
    falls back to its cold-start catalog (which still carries starter).

    Uses ``credit_usd=0`` (VPS-only) to bypass the credit-allowlist guard.
    """
    import logging
    from app import main as main_mod
    monkeypatch.setattr(main_mod.settings, "mintoffice_api_key", "")
    with caplog.at_level(logging.ERROR, logger="partner_portal"):
        resp = client.post(
            "/buy",
            data={"plan": "starter", "credit_usd": "0"},
            follow_redirects=False,
        )
    assert resp.status_code == 503, resp.text
    assert "temporarily unavailable" in resp.text
    assert "could not reach checkout" not in resp.text.lower()
    assert any(
        "Portal misconfigured" in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]


def test_buy_post_401_logs_credential_failure(client, httpx_mock, caplog):
    """Regression — a revoked/mistyped key returns 401. Customer sees the
    maintenance message (503); the operator sees a flagged log line."""
    import logging
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10]},
        status_code=200,
    )
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/orders",
        json={"error": {"code": "invalid_api_key", "message": "Rotate from the MintOffice dashboard"}},
        status_code=401,
    )
    with caplog.at_level(logging.ERROR, logger="partner_portal"):
        resp = client.post(
            "/buy",
            data={"plan": "starter", "credit_usd": "10"},
            follow_redirects=False,
        )
    assert resp.status_code == 503, resp.text
    assert "temporarily unavailable" in resp.text
    assert any(
        "rejected our credentials" in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]


def test_credit_cache_sticky_on_failure(client, httpx_mock):
    """Regression — if the first settings fetch succeeds and the next
    fails, the cache keeps the last good value instead of poisoning
    itself with an empty list. (The catalog is cached separately and
    stays warm across both /buy calls.)"""
    _add_catalog(httpx_mock)
    # First request: good answer.
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20]},
        status_code=200,
    )
    r1 = client.get("/buy")
    assert r1.status_code == 200
    assert 'value="10"' in r1.text
    assert 'value="20"' in r1.text

    # Force the cached credit value to look stale so the next /buy refetches.
    main_mod = sys.modules.get("app.main")
    assert main_mod is not None
    main_mod._credit_options_cache["expires_at"] = 0.0

    # Second request: MintOffice 502s on settings.
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        status_code=502,
    )
    r2 = client.get("/buy")
    assert r2.status_code == 200
    # Sticky: prior options still rendered, NOT the VPS-only fallback.
    assert 'value="10"' in r2.text
    assert 'value="20"' in r2.text
    assert 'type="hidden" name="credit_usd"' not in r2.text
