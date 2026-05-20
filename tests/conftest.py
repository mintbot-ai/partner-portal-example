"""Shared pytest fixtures.

Environment is set up BEFORE app modules import so ``settings`` snapshots
the right values. The portal SQLite file lives in a tmp dir so tests
don't trample each other or leave junk behind.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def _set_env(monkeypatch, tmp_path, *, mintoffice_url: str, retries: str = "2") -> None:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "portal.db"))
    monkeypatch.setenv("PARTNER_BRAND", "AcmeAI")
    monkeypatch.setenv("MINTOFFICE_API_URL", mintoffice_url)
    monkeypatch.setenv("MINTOFFICE_API_KEY", "mo_live_testkey")
    monkeypatch.setenv("MINTOFFICE_WEBHOOK_SECRET", "whsec_testsecret")
    monkeypatch.setenv("MINTOFFICE_RETRIES", retries)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://acmeai.example.test")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    monkeypatch.setenv("ADMIN_PAGE_SIZE", "5")


def _reload_app():
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app.main as main_mod  # noqa: WPS433
    main_mod.db.init_db()
    return main_mod


@pytest.fixture
def app_with_tmp_db(tmp_path, monkeypatch):
    """Reload the app package against a tmp DB and fixed secrets."""
    _set_env(monkeypatch, tmp_path, mintoffice_url="https://mint.example.test")
    return _reload_app()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient wired to a fresh app instance.

    Uses ``http://mintoffice.test`` so pytest-httpx mocks can match the
    exact ``GET /api/v1/settings`` and ``POST /api/v1/orders`` URLs.
    """
    _set_env(monkeypatch, tmp_path, mintoffice_url="http://mintoffice.test", retries="0")
    main_mod = _reload_app()
    from fastapi.testclient import TestClient
    return TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def _reset_buy_cache():
    """Wipe the per-process credit-options cache between tests.

    The /buy GET caches ``allowed_credit_options`` after the first call so
    the partner-settings request doesn't fire on every page load. Tests
    use different mocked responses, so the cache must reset.
    """
    main_mod = sys.modules.get("app.main")
    if main_mod is not None and hasattr(main_mod, "_credit_options_cache"):
        main_mod._credit_options_cache = None
    yield
    main_mod = sys.modules.get("app.main")
    if main_mod is not None and hasattr(main_mod, "_credit_options_cache"):
        main_mod._credit_options_cache = None
