import sys

import pytest
from unittest.mock import patch


def test_buy_shows_credit_options_from_partner_settings(client, httpx_mock):
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


def test_buy_no_credit_options_shows_hidden_input(client, httpx_mock):
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
    """Regression — the earlier assertion just checked that `checked`
    appeared anywhere on the page, but the credit radios are *also*
    `checked` by default (first option). Tighten the check so it
    actually verifies the s1 plan radio is the selected one."""
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20, 50]},
        status_code=200,
    )
    resp = client.get("/buy?plan=s1")
    assert resp.status_code == 200
    # The s1 plan radio carries the checked attribute.
    assert 'name="plan" value="s1"' in resp.text
    assert 'name="plan" value="s1" required' in resp.text  # sanity
    s1_radio_idx = resp.text.find('name="plan" value="s1"')
    # Look at the next 200 chars after the s1 radio for the checked
    # attribute — the template appends it inline on the same <input>.
    snippet = resp.text[s1_radio_idx : s1_radio_idx + 200]
    assert " checked" in snippet, snippet
    # And confirm no *other* plan radio carries checked.
    for other in ("trial", "s2"):
        idx = resp.text.find(f'name="plan" value="{other}"')
        assert idx >= 0
        assert " checked" not in resp.text[idx : idx + 200]


def test_buy_post_with_credit_usd(client, httpx_mock):
    # Partner settings fetched once when the POST validates credit_usd.
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20, 50]},
        status_code=200,
    )
    httpx_mock.add_response(json={"checkout_url": "https://stripe.test/pay"}, status_code=200)
    resp = client.post("/buy", data={"plan": "trial", "credit_usd": "10", "idempotency_key_form": "key123"}, follow_redirects=False)
    assert resp.status_code == 303


def test_buy_post_unknown_plan(client):
    resp = client.post("/buy", data={"plan": "unknown", "credit_usd": "0"})
    assert resp.status_code == 422


def test_buy_post_rejects_disallowed_credit_usd(client, httpx_mock):
    """Server-side allow-list — the form lets the customer pick from
    [10, 20] but a hand-crafted POST with credit_usd=50 must 400, not
    pass through to MintOffice."""
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20]},
        status_code=200,
    )
    resp = client.post(
        "/buy",
        data={"plan": "s1", "credit_usd": "50"},
        follow_redirects=False,
    )
    assert resp.status_code == 400, resp.text
    assert "isn't available" in resp.text or "isn&#39;t available" in resp.text


def test_buy_post_accepts_zero_credit_even_when_options_set(client, httpx_mock):
    """0 (VPS-only) is always allowed regardless of the partner's
    bundle list, because the /buy template renders a dedicated
    'No credit — VPS only' radio."""
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
        data={"plan": "s1", "credit_usd": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_credit_cache_sticky_on_failure(client, httpx_mock):
    """Regression — if the first MintOffice fetch succeeds and the next
    one fails, the cache must keep the previously-seen good value
    instead of poisoning itself with an empty list."""
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

    # Force the cached value to look stale so the next /buy refetches.
    main_mod = sys.modules.get("app.main")
    assert main_mod is not None
    main_mod._credit_options_cache["expires_at"] = 0.0

    # Second request: MintOffice 502s.
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        status_code=502,
    )
    r2 = client.get("/buy")
    assert r2.status_code == 200
    # Sticky: prior options still rendered, NOT the VPS-only fallback.
    assert 'value="10"' in r2.text
    assert 'value="20"' in r2.text
    # The hidden-fallback ``<input type="hidden" name="credit_usd"`` only
    # appears when the cache is empty (zero options). It must not appear
    # here — that would mean the cache got poisoned.
    assert 'type="hidden" name="credit_usd"' not in r2.text
