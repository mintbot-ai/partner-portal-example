"""Tests for the /extend subscription sign-up flow.

Mirrors the structure of ``test_buy.py`` — same fixtures, same httpx
mock contract. /extend is intentionally simpler than /buy because there
is no credit-bundle picker (the recurring monthly base is the only
line item) and no per-plan duration (every plan recurs monthly).
"""
from __future__ import annotations

import json as _json


def test_extend_get_renders_form(client):
    resp = client.get("/extend")
    assert resp.status_code == 200
    # All subscription plans appear as radios.
    assert 'name="plan" value="s1"' in resp.text
    assert 'name="plan" value="s2"' in resp.text
    # Email + language fields are present.
    assert 'name="email"' in resp.text
    assert 'name="language"' in resp.text


def test_extend_get_preselects_plan_from_query(client):
    resp = client.get("/extend?tier=s2")
    assert resp.status_code == 200
    s2_idx = resp.text.find('name="plan" value="s2"')
    assert s2_idx >= 0
    assert " checked" in resp.text[s2_idx : s2_idx + 200]


def test_extend_get_ignores_unknown_tier(client):
    resp = client.get("/extend?tier=bogus")
    assert resp.status_code == 200
    # No plan is pre-selected — none of the radios carry checked.
    for slug in ("s1", "s2"):
        idx = resp.text.find(f'name="plan" value="{slug}"')
        assert idx >= 0
        assert " checked" not in resp.text[idx : idx + 200]


def test_extend_get_prefills_email_and_lang(client):
    resp = client.get("/extend?email=user@example.com&lang=et")
    assert resp.status_code == 200
    assert 'value="user@example.com"' in resp.text
    # Estonian option is selected.
    et_idx = resp.text.find('value="et"')
    assert et_idx >= 0
    assert "selected" in resp.text[et_idx : et_idx + 30]


def test_extend_post_redirects_to_stripe(client, httpx_mock):
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
        data={"plan": "s2", "email": "buyer@example.com", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "https://stripe.test/sub_checkout"


def test_extend_post_sends_expected_body_to_mintoffice(client, httpx_mock):
    """Regression — body shape MUST match the MintOffice
    /subscriptions schema (tier, currency, language, success/cancel
    URLs). Extra or missing fields surface as 422 in production."""
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/subscriptions",
        json={"checkout_url": "https://stripe.test/x"},
        status_code=200,
    )
    resp = client.post(
        "/extend",
        data={"plan": "s1", "email": "a@b.c", "language": "et"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    calls = [
        r for r in httpx_mock.get_requests()
        if r.url.path.endswith("/subscriptions") and r.method == "POST"
    ]
    assert len(calls) == 1
    body = _json.loads(calls[0].content.decode())
    assert body["tier"] == "s1"
    assert body["currency"] == "usd"
    assert body["language"] == "et"
    assert body["customer_email"] == "a@b.c"
    assert body["success_url"].startswith("https://exampleai.example.test/thank-you")
    assert body["cancel_url"].startswith("https://exampleai.example.test/cancel")
    assert body["product_name"].startswith("ExampleAI ")
    assert "duration_months" not in body  # subscription, not order
    assert "credit_usd" not in body


def test_extend_post_unknown_plan(client):
    resp = client.post("/extend", data={"plan": "trial", "language": "en"})
    assert resp.status_code == 422


def test_extend_post_rejects_malformed_email(client):
    """Server-side check — the form accepts ``type=email`` but a curl
    bypass with no @ must 400 rather than hit MintOffice."""
    resp = client.post(
        "/extend",
        data={"plan": "s1", "email": "not-an-email", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "email" in resp.text.lower()


def test_extend_post_surfaces_mintoffice_422(client, httpx_mock):
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/subscriptions",
        json={"error": {"code": "VALIDATION_ERROR", "message": "tier not allowed"}},
        status_code=422,
    )
    resp = client.post(
        "/extend",
        data={"plan": "s2", "email": "a@b.c", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    assert "tier not allowed" in resp.text


def test_extend_post_handles_missing_checkout_url(client, httpx_mock):
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/subscriptions",
        json={"id": 1, "status": "pending", "checkout_url": ""},
        status_code=200,
    )
    resp = client.post(
        "/extend",
        data={"plan": "s2", "email": "a@b.c", "language": "en"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    assert "didn&#39;t return" in resp.text or "didn't return" in resp.text
