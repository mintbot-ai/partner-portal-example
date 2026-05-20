"""Tests for the /buy form → MintOffice → Stripe redirect flow.

httpx is mocked at the module level so no network goes out.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _stub_mintoffice(monkeypatch, app_mod, *, status_code=200, body=None):
    """Replace ``app.mintoffice.create_order`` with a stub returning a
    deterministic order. Captures kwargs for assertions."""
    captured: dict = {}

    def fake_create_order(**kwargs):
        captured.update(kwargs)
        if status_code >= 400:
            raise app_mod.mintoffice.MintOfficeError(status_code, body)
        return app_mod.mintoffice.CreatedOrder(
            id=123,
            status="pending",
            checkout_url="https://checkout.stripe.test/cs_test_abc",
            amount_cents=999,
            currency="usd",
        )

    monkeypatch.setattr(app_mod.mintoffice, "create_order", fake_create_order)
    return captured


def test_buy_redirects_to_stripe(app_with_tmp_db, monkeypatch):
    captured = _stub_mintoffice(monkeypatch, app_with_tmp_db)
    client = TestClient(app_with_tmp_db.app, follow_redirects=False)
    r = client.post("/buy", data={"plan": "pro", "language": "en"})
    assert r.status_code == 303
    assert r.headers["location"] == "https://checkout.stripe.test/cs_test_abc"
    # Captured kwargs include the branded product name.
    assert captured["tier"] == "s2"
    assert captured["duration_days"] == 30
    assert "AcmeAI" in captured["product_name"]
    assert captured["success_url"].startswith("https://acmeai.example.test/thank-you")


def test_buy_invalid_plan_renders_error(app_with_tmp_db, monkeypatch):
    _stub_mintoffice(monkeypatch, app_with_tmp_db)
    client = TestClient(app_with_tmp_db.app)
    r = client.post("/buy", data={"plan": "platinum", "language": "en"})
    assert r.status_code == 400
    assert "valid plan" in r.text.lower()


def test_buy_surfaces_mintoffice_error_message(app_with_tmp_db, monkeypatch):
    _stub_mintoffice(
        monkeypatch, app_with_tmp_db,
        status_code=409,
        body={"error": {"code": "unpriced_tier", "message": "tier 's4' has no default pricing"}},
    )
    client = TestClient(app_with_tmp_db.app)
    r = client.post("/buy", data={"plan": "pro", "language": "en"})
    assert r.status_code == 502
    assert "unpriced_tier" in r.text or "no default pricing" in r.text


def test_landing_lists_all_plans(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/")
    assert r.status_code == 200
    for label in ("Trial", "Basic", "Pro"):
        assert label in r.text


def test_buy_pre_selects_plan_from_query_string(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/buy?plan=basic")
    assert r.status_code == 200
    # The matching radio input is pre-checked.
    assert 'value="basic"' in r.text and 'checked' in r.text


def test_buy_query_string_with_unknown_plan_is_ignored(app_with_tmp_db):
    """An unknown ``?plan=`` slug must not blow up — just render the form
    with no radio attribute-checked so the customer can still pick."""
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/buy?plan=does-not-exist")
    assert r.status_code == 200
    # No <input ... checked> on any plan radio.
    assert "checked>" not in r.text and 'checked "' not in r.text


def test_admin_requires_basic_auth(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/admin")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("basic")


def test_admin_renders_events_with_valid_creds(app_with_tmp_db, monkeypatch):
    # Seed one event via the DB helper directly.
    app_with_tmp_db.db.store_event(
        event_id="evt_seed_1",
        event_type="order.paid",
        payload='{"order_id": 1}',
    )
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/admin", auth=("admin", "test-pw"))
    assert r.status_code == 200
    assert "evt_seed_1" in r.text
    assert "order.paid" in r.text
