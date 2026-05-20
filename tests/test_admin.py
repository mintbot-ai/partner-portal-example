"""Tests for /admin pagination + event_type filter + UI rendering."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _seed(app_mod, n: int, *, event_type: str = "order.paid") -> None:
    for i in range(n):
        app_mod.db.store_event(
            event_id=f"evt_{event_type}_{i}",
            event_type=event_type,
            payload=f'{{"order_id": {i}}}',
        )


def test_admin_paginates_when_more_than_page_size(app_with_tmp_db):
    # ADMIN_PAGE_SIZE is monkeypatched to 5 in conftest, so 12 events
    # crosses the page boundary.
    _seed(app_with_tmp_db, 12)
    client = TestClient(app_with_tmp_db.app)

    r = client.get("/admin", auth=("admin", "test-pw"))
    assert r.status_code == 200
    assert "evt_order.paid_11" in r.text
    assert "offset=5" in r.text

    r2 = client.get("/admin?offset=5", auth=("admin", "test-pw"))
    assert r2.status_code == 200
    assert "evt_order.paid_6" in r2.text
    assert "back to newest" in r2.text


def test_admin_filters_by_event_type(app_with_tmp_db):
    _seed(app_with_tmp_db, 3, event_type="order.paid")
    _seed(app_with_tmp_db, 2, event_type="agent.ready")
    client = TestClient(app_with_tmp_db.app)

    r = client.get("/admin?type=agent.ready", auth=("admin", "test-pw"))
    assert r.status_code == 200
    assert "evt_agent.ready_0" in r.text
    assert "evt_agent.ready_1" in r.text
    assert "evt_order.paid_0" not in r.text
    # Filter chip visible.
    assert "Clear" in r.text


def test_admin_empty_state_when_no_events(app_with_tmp_db):
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/admin", auth=("admin", "test-pw"))
    assert r.status_code == 200
    assert "No events" in r.text


def test_admin_event_type_dropdown_lists_known_types(app_with_tmp_db):
    _seed(app_with_tmp_db, 1, event_type="agent.ready")
    _seed(app_with_tmp_db, 1, event_type="order.paid")
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/admin", auth=("admin", "test-pw"))
    assert r.status_code == 200
    # Both types appear as <option> values in the filter dropdown.
    assert 'value="agent.ready"' in r.text
    assert 'value="order.paid"' in r.text


def test_admin_payload_collapsible_summary_present(app_with_tmp_db):
    """Payload should be wrapped in a <details> so the page stays scannable
    even with hundreds of large events."""
    _seed(app_with_tmp_db, 1)
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/admin", auth=("admin", "test-pw"))
    assert r.status_code == 200
    assert "<details>" in r.text
    assert "view payload" in r.text


def test_admin_event_type_query_clamped(app_with_tmp_db):
    """Pathological inputs (very long ?type=) are rejected via FastAPI's
    max_length validator — no SQL gets through."""
    client = TestClient(app_with_tmp_db.app)
    r = client.get("/admin?type=" + "x" * 500, auth=("admin", "test-pw"))
    assert r.status_code == 422
