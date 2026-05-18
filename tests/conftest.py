"""Shared pytest fixtures.

Environment is set up BEFORE app modules import so ``settings`` snapshots
the right values. The portal SQLite file lives in a tmp dir so tests
don't trample each other or leave junk behind.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def app_with_tmp_db(tmp_path, monkeypatch):
    """Reload the app package against a tmp DB and fixed secrets.

    Each test gets a fresh SQLite file at ``tmp_path/portal.db``. We
    re-import the relevant modules so ``settings`` reflects the env
    we just monkeypatched, rather than whatever ``conftest`` saw on
    its first import.
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "portal.db"))
    monkeypatch.setenv("PARTNER_BRAND", "AcmeAI")
    monkeypatch.setenv("MINTOFFICE_API_URL", "https://mint.example.test")
    monkeypatch.setenv("MINTOFFICE_API_KEY", "mo_live_testkey")
    monkeypatch.setenv("MINTOFFICE_WEBHOOK_SECRET", "whsec_testsecret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://acmeai.example.test")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")

    # Force a fresh import of the app package so settings are re-read.
    for mod in list(sys.modules):
        if mod.startswith("app"):
            del sys.modules[mod]

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import app.main as main_mod  # noqa: WPS433
    main_mod.db.init_db()
    return main_mod
