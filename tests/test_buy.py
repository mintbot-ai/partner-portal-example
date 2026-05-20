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
    httpx_mock.add_response(
        url="http://mintoffice.test/api/v1/settings",
        json={"allowed_credit_options": [10, 20, 50]},
        status_code=200,
    )
    resp = client.get("/buy?plan=s1")
    assert resp.status_code == 200
    assert 'checked' in resp.text


def test_buy_post_with_credit_usd(client, httpx_mock):
    httpx_mock.add_response(json={"checkout_url": "https://stripe.test/pay"}, status_code=200)
    resp = client.post("/buy", data={"plan": "trial", "credit_usd": "10", "idempotency_key_form": "key123"}, follow_redirects=False)
    assert resp.status_code == 303


def test_buy_post_unknown_plan(client):
    resp = client.post("/buy", data={"plan": "unknown", "credit_usd": "0"})
    assert resp.status_code == 422
