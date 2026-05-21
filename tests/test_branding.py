"""Cosmetic / safety-net rendering tests — TEST-mode banner."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_app(monkeypatch, tmp_path, **env):
    """Re-import the app package against the given env so module-scope
    template globals (``test_mode``) reflect the test's settings."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "portal.db"))
    monkeypatch.setenv("PARTNER_BRAND", env.get("PARTNER_BRAND", "ExampleAI"))
    monkeypatch.setenv("MINTOFFICE_API_URL", env["MINTOFFICE_API_URL"])
    monkeypatch.setenv("MINTOFFICE_API_KEY", "mo_live_testkey")
    monkeypatch.setenv("MINTOFFICE_WEBHOOK_SECRET", "whsec_testsecret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://exampleai.example.test")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")

    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app.main as main_mod  # noqa: WPS433
    main_mod.db.init_db()
    return main_mod


def test_test_mode_banner_shows_for_dev_api(monkeypatch, tmp_path):
    main_mod = _fresh_app(
        monkeypatch, tmp_path, MINTOFFICE_API_URL="https://mint.mintbot.dev",
    )
    client = TestClient(main_mod.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "TEST MODE" in r.text
    assert "mint.mintbot.dev" in r.text


def test_test_mode_banner_absent_for_live_api(monkeypatch, tmp_path):
    main_mod = _fresh_app(
        monkeypatch, tmp_path, MINTOFFICE_API_URL="https://mint.mintbot.ai",
    )
    client = TestClient(main_mod.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "TEST MODE" not in r.text


def test_test_mode_banner_renders_on_buy_page_too(monkeypatch, tmp_path):
    """The banner is in base.html, so it should appear on every page that
    extends it — verify /buy as a representative second route."""
    main_mod = _fresh_app(
        monkeypatch, tmp_path, MINTOFFICE_API_URL="https://mint.mintbot.dev",
    )
    client = TestClient(main_mod.app)
    r = client.get("/buy")
    assert r.status_code == 200
    assert "TEST MODE" in r.text
