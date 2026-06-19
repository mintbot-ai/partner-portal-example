"""Tests for the /extend subscription sign-up flow.

Like /buy, the subscription cards now come from the live catalog
(GET /api/v1/catalog) — the subscription-capable public packages
(starter/pro; trial is excluded server-side as a one-day evaluation).
Every test that renders /extend registers a catalog response.
"""
from __future__ import annotations

import json as _json


# Subscription-capable packages carry subscription.available = true. trial
# is present but not subscription-capable, so it must not appear on /extend.
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
         "durations": [{"months": 1, "label": "1 month", "price_cents": 1500}],
         "subscription": {"available": True, "price_cents": 1500}},
        {"tier": "pro", "display_name": "Pro",
         "description": "Faster model, longer context.",
         "featured": False, "default_credit_usd": 0,
         "durations": [{"months": 1, "label": "1 month", "price_cents": 3900}],
         "subscription": {"available": True, "price_cents": 3900}},
    ],
}


def _add_catalog(httpx_mock):
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/catalog",
        json=CATALOG,
        status_code=200,
    )


def test_extend_get_renders_form(client, httpx_mock):
    _add_catalog(httpx_mock)
    resp = client.get("/extend")
    assert resp.status_code == 200
    # Subscription-capable public plans appear as radios.
    assert 'name="plan" value="starter"' in resp.text
    assert 'name="plan" value="pro"' in resp.text
    # trial is not a recurring plan — and the retired slugs are gone.
    assert 'name="plan" value="trial"' not in resp.text
    for dead in ("s1", "s2", "s4"):
        assert f'name="plan" value="{dead}"' not in resp.text
    assert 'name="email"' in resp.text
    assert 'name="language"' in resp.text


def test_extend_get_preselects_plan_from_query(client, httpx_mock):
    _add_catalog(httpx_mock)
    resp = client.get("/extend?tier=pro")
    assert resp.status_code == 200
    idx = resp.text.find('name="plan" value="pro"')
    assert idx >= 0
    assert " checked" in resp.text[idx : idx + 200]


def test_extend_get_ignores_unknown_tier(client, httpx_mock):
    _add_catalog(httpx_mock)
    resp = client.get("/extend?tier=bogus")
    assert resp.status_code == 200
    for slug in ("starter", "pro"):
        idx = resp.text.find(f'name="plan" value="{slug}"')
        assert idx >= 0
        assert " checked" not in resp.text[idx : idx + 200]


def test_extend_get_prefills_email_and_lang(client, httpx_mock):
    _add_catalog(httpx_mock)
    resp = client.get("/extend?email=user@example.com&lang=et")
    assert resp.status_code == 200
    assert 'value="user@example.com"' in resp.text
    et_idx = resp.text.find('value="et"')
    assert et_idx >= 0
    assert "selected" in resp.text[et_idx : et_idx + 30]


def test_extend_post_redirects_to_stripe(client, httpx_mock):
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/subscriptions",
        json={
            "id": 42,
            "status": "pending",
            "checkout_url": "https://stripe.test/sub_checkout",
            "amount_cents": 3900,
            "currency": "usd",
        },
        status_code=200,
    )
    resp = client.post(
        "/extend",
        data={"plan": "pro", "email": "buyer@example.com", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "https://stripe.test/sub_checkout"


def test_extend_post_sends_expected_body_to_mintoffice(client, httpx_mock):
    """Regression — body shape MUST match the MintOffice /subscriptions
    schema (tier, currency, language, success/cancel URLs)."""
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/subscriptions",
        json={"checkout_url": "https://stripe.test/x"},
        status_code=200,
    )
    resp = client.post(
        "/extend",
        data={"plan": "starter", "email": "a@b.c", "language": "et"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    calls = [
        r for r in httpx_mock.get_requests()
        if r.url.path.endswith("/subscriptions") and r.method == "POST"
    ]
    assert len(calls) == 1
    body = _json.loads(calls[0].content.decode())
    assert body["tier"] == "starter"
    assert body["currency"] == "usd"
    assert body["language"] == "et"
    assert body["customer_email"] == "a@b.c"
    assert body["success_url"].startswith("https://exampleai.example.test/thank-you")
    assert body["cancel_url"].startswith("https://exampleai.example.test/cancel")
    assert body["product_name"].startswith("ExampleAI ")
    assert "duration_months" not in body  # subscription, not order
    assert "credit_usd" not in body


def test_extend_post_unknown_plan(client, httpx_mock):
    """trial is not subscription-capable, so it's not a valid /extend plan."""
    _add_catalog(httpx_mock)
    resp = client.post("/extend", data={"plan": "trial", "language": "en"})
    assert resp.status_code == 422


def test_extend_post_rejects_malformed_email(client, httpx_mock):
    """Server-side check — a curl bypass with no @ must 400 rather than
    hit MintOffice."""
    _add_catalog(httpx_mock)
    resp = client.post(
        "/extend",
        data={"plan": "starter", "email": "not-an-email", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "email" in resp.text.lower()


def test_extend_post_surfaces_mintoffice_422(client, httpx_mock):
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/subscriptions",
        json={"error": {"code": "VALIDATION_ERROR", "message": "tier not allowed"}},
        status_code=422,
    )
    resp = client.post(
        "/extend",
        data={"plan": "pro", "email": "a@b.c", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    assert "tier not allowed" in resp.text


def test_extend_post_handles_missing_checkout_url(client, httpx_mock):
    _add_catalog(httpx_mock)
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/subscriptions",
        json={"id": 1, "status": "pending", "checkout_url": ""},
        status_code=200,
    )
    resp = client.post(
        "/extend",
        data={"plan": "pro", "email": "a@b.c", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    assert "didn&#39;t return" in resp.text or "didn't return" in resp.text
