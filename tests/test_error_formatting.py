"""Tests for ``mintoffice.format_error`` — the helper that turns a
``MintOfficeError`` body into something useful to show a customer.

MintOffice emits two distinct shapes for failures, and partners hit both
in practice. The portal must surface the actual reason, not a generic
``MintOffice returned 422.`` line that sends them straight to grep the
server logs.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import mintoffice


def _err(status_code: int, body) -> mintoffice.MintOfficeError:
    return mintoffice.MintOfficeError(status_code, body)


def test_format_error_extracts_business_error_message():
    msg = mintoffice.format_error(_err(
        409, {"error": {"code": "unpriced_tier", "message": "tier 's4' has no default pricing"}},
    ))
    assert msg == "tier 's4' has no default pricing"


def test_format_error_extracts_fastapi_validation_detail():
    """FastAPI 422 — surfaces field + msg for each validation failure."""
    msg = mintoffice.format_error(_err(422, {
        "detail": [
            {"type": "value_error", "loc": ["body", "success_url"],
             "msg": "Value error, success_url must be https"},
            {"type": "value_error", "loc": ["body", "cancel_url"],
             "msg": "Value error, cancel_url must be https"},
        ],
    }))
    assert "success_url" in msg
    assert "must be https" in msg
    assert "cancel_url" in msg
    assert " · " in msg


def test_format_error_handles_string_detail():
    msg = mintoffice.format_error(_err(404, {"detail": "Not Found"}))
    assert msg == "Not Found"


def test_format_error_falls_back_to_status_code():
    msg = mintoffice.format_error(_err(503, None))
    assert msg == "MintOffice returned 503."

    msg = mintoffice.format_error(_err(500, {"weird": "shape"}))
    assert msg == "MintOffice returned 500."


def test_buy_surfaces_fastapi_validation_message(app_with_tmp_db, monkeypatch):
    """End-to-end: a FastAPI-shaped 422 from MintOffice gets rendered
    on the /buy form instead of the opaque status-code fallback."""
    def fake_create_order(**kwargs):
        raise app_with_tmp_db.mintoffice.MintOfficeError(422, {
            "detail": [
                {"loc": ["body", "success_url"],
                 "msg": "Value error, success_url must be https"},
            ],
        })

    monkeypatch.setattr(
        app_with_tmp_db.mintoffice, "create_order", fake_create_order,
    )
    client = TestClient(app_with_tmp_db.app)
    r = client.post("/buy", data={"plan": "trial", "language": "en"})
    assert r.status_code == 502
    assert "success_url" in r.text
    assert "must be https" in r.text
