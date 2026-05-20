"""Tests for security headers, /healthz, branded error pages, and
``mintoffice._post_with_retry`` backoff semantics."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient


def test_security_headers_present_on_landing(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_healthz_reports_db_state(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["checks"]["db"] is True
    assert body["checks"]["mintoffice_api_key"] == "configured"
    assert body["version"]
    assert body["brand"]


def test_healthz_503_when_db_broken(app_with_tmp_db, monkeypatch):
    monkeypatch.setattr(app_with_tmp_db.db, "healthcheck", lambda: False)
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["db"] is False


def test_healthz_503_when_mintoffice_api_key_missing(app_with_tmp_db, monkeypatch):
    """Regression — the rotation-cleared-.env incident on agent99.cc.
    A monitor that only watches the landing page misses this; /healthz
    must surface it as a hard 503 so generic uptime alerts fire."""
    monkeypatch.setattr(app_with_tmp_db.settings, "mintoffice_api_key", "")
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["db"] is True
    assert body["checks"]["mintoffice_api_key"] == "missing"


def test_404_renders_branded_html_for_browsers(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/no-such-page", headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert "<html" in r.text.lower()
    assert "AcmeAI" in r.text
    assert "404" in r.text


def test_404_returns_json_for_api_clients(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/no-such-page", headers={"Accept": "application/json"})
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "Page not found"


def test_post_with_retry_succeeds_after_one_503(monkeypatch, app_with_tmp_db):
    """The MintOffice client retries idempotent POSTs on 5xx — once we
    return 200 the call resolves normally."""
    from app import mintoffice

    calls = {"n": 0}

    class StubResp:
        def __init__(self, status_code):
            self.status_code = status_code
        def json(self):
            return {"ok": True}

    class StubClient:
        def post(self, path, json, headers):
            calls["n"] += 1
            if calls["n"] == 1:
                return StubResp(503)
            return StubResp(200)

    monkeypatch.setattr(mintoffice.time, "sleep", lambda _s: None)
    r = mintoffice._post_with_retry(
        StubClient(), "/orders", json={}, headers={"Idempotency-Key": "k"},
    )
    assert r.status_code == 200
    assert calls["n"] == 2


def test_post_with_retry_gives_up_after_exhausting_attempts(monkeypatch, app_with_tmp_db):
    from app import mintoffice

    calls = {"n": 0}

    class StubResp:
        status_code = 502
        def json(self):
            return {}

    class StubClient:
        def post(self, path, json, headers):
            calls["n"] += 1
            return StubResp()

    monkeypatch.setattr(mintoffice.time, "sleep", lambda _s: None)
    monkeypatch.setattr(mintoffice.settings, "mintoffice_retries", 2)  # 3 attempts total
    r = mintoffice._post_with_retry(
        StubClient(), "/orders", json={}, headers={"Idempotency-Key": "k"},
    )
    assert r.status_code == 502
    assert calls["n"] == 3


def test_post_with_retry_raises_on_persistent_transport_error(monkeypatch, app_with_tmp_db):
    from app import mintoffice

    class StubClient:
        def post(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(mintoffice.time, "sleep", lambda _s: None)
    monkeypatch.setattr(mintoffice.settings, "mintoffice_retries", 1)
    with pytest.raises(httpx.ConnectError):
        mintoffice._post_with_retry(
            StubClient(), "/orders", json={}, headers={"Idempotency-Key": "k"},
        )


def test_landing_renders_prices_for_priced_plans(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/")
    assert r.status_code == 200
    # Pro plan ships with price_cents=3900 → "$39" on the card.
    assert "$39" in r.text
    # Trial is free.
    assert "Free" in r.text
    # Recommended ribbon present on the featured plan.
    assert "Recommended" in r.text
