"""Tests for the inbound /webhooks/mintoffice signature contract."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

SECRET = "whsec_testsecret"


def _sign(body: bytes, ts: int) -> str:
    mac = hmac.new(
        SECRET.encode("utf-8"),
        f"{ts}.".encode("utf-8") + body,
        hashlib.sha256,
    )
    return f"t={ts},v1={mac.hexdigest()}"


def test_valid_signed_event_is_stored(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    body = json.dumps({"order_id": 42, "tier": "s2"}).encode("utf-8")
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-Mintbot-Signature": _sign(body, ts),
        "X-Mintbot-Event-Id": "evt_42_order_paid_1700000000000",
        "X-Mintbot-Event-Type": "order.paid",
    }
    r = client.post("/webhooks/mintoffice", content=body, headers=headers)
    assert r.status_code == 200

    events = app_with_tmp_db.db.list_events()
    assert len(events) == 1
    assert events[0]["event_type"] == "order.paid"
    assert json.loads(events[0]["payload"])["order_id"] == 42


def test_replay_with_same_event_id_is_idempotent(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    body = b'{"order_id": 1}'
    ts = int(time.time())
    headers = {
        "X-Mintbot-Signature": _sign(body, ts),
        "X-Mintbot-Event-Id": "evt_1_order_created_1700000000000",
        "X-Mintbot-Event-Type": "order.created",
    }
    r1 = client.post("/webhooks/mintoffice", content=body, headers=headers)
    r2 = client.post("/webhooks/mintoffice", content=body, headers=headers)
    assert r1.status_code == r2.status_code == 200
    assert len(app_with_tmp_db.db.list_events()) == 1


def test_bad_signature_is_rejected(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    body = b'{"order_id": 1}'
    ts = int(time.time())
    headers = {
        "X-Mintbot-Signature": f"t={ts},v1=" + ("0" * 64),
        "X-Mintbot-Event-Id": "evt_1_order_created_x",
        "X-Mintbot-Event-Type": "order.created",
    }
    r = client.post("/webhooks/mintoffice", content=body, headers=headers)
    assert r.status_code == 401
    assert app_with_tmp_db.db.list_events() == []


def test_stale_timestamp_is_rejected(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    body = b'{"order_id": 1}'
    ts = int(time.time()) - 3600  # an hour old → way past the 5min tolerance
    headers = {
        "X-Mintbot-Signature": _sign(body, ts),
        "X-Mintbot-Event-Id": "evt_x",
        "X-Mintbot-Event-Type": "order.created",
    }
    r = client.post("/webhooks/mintoffice", content=body, headers=headers)
    assert r.status_code == 401


def test_missing_signature_header_is_rejected(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.post(
        "/webhooks/mintoffice",
        content=b'{"order_id": 1}',
        headers={
            "X-Mintbot-Event-Id": "evt_x",
            "X-Mintbot-Event-Type": "order.created",
        },
    )
    assert r.status_code == 401


def test_signature_covers_raw_body_not_canonical_json(app_with_tmp_db):
    """The signature is over the literal bytes of the request body —
    re-serializing JSON on the client side would change whitespace and
    invalidate the HMAC. Verify that's exactly what the verifier does."""
    client = TestClient(app_with_tmp_db.app)
    body = b'{"order_id":7,"tier":"s1"}'  # tight spacing
    ts = int(time.time())
    headers = {
        "X-Mintbot-Signature": _sign(body, ts),
        "X-Mintbot-Event-Id": "evt_raw_1",
        "X-Mintbot-Event-Type": "order.paid",
    }
    r = client.post("/webhooks/mintoffice", content=body, headers=headers)
    assert r.status_code == 200

    # Now send the SAME parsed payload re-serialised with extra whitespace —
    # signature should fail.
    reserialized = b'{"order_id": 7, "tier": "s1"}'
    headers2 = {**headers, "X-Mintbot-Event-Id": "evt_raw_2"}
    r2 = client.post("/webhooks/mintoffice", content=reserialized, headers=headers2)
    assert r2.status_code == 401
