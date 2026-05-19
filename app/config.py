"""Environment-driven configuration for the example partner portal.

Read once at import; values are plain attributes on a dataclass so
tests can monkeypatch a fresh instance without an env-var dance.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    partner_brand: str
    mintoffice_api_url: str
    mintoffice_api_key: str
    mintoffice_webhook_secret: str
    public_base_url: str
    admin_username: str
    admin_password: str
    db_path: str


def _load() -> Settings:
    return Settings(
        partner_brand=os.environ.get("PARTNER_BRAND", "AcmeAI").strip() or "AcmeAI",
        mintoffice_api_url=os.environ.get(
            "MINTOFFICE_API_URL", "https://mint.mintbot.dev"
        ).rstrip("/"),
        mintoffice_api_key=os.environ.get("MINTOFFICE_API_KEY", "").strip(),
        mintoffice_webhook_secret=os.environ.get(
            "MINTOFFICE_WEBHOOK_SECRET", ""
        ).strip(),
        public_base_url=os.environ.get(
            "PUBLIC_BASE_URL", "http://127.0.0.1:8000"
        ).rstrip("/"),
        admin_username=os.environ.get("ADMIN_USERNAME", "admin").strip(),
        admin_password=os.environ.get("ADMIN_PASSWORD", "").strip(),
        db_path=os.environ.get("DB_PATH", "portal.db").strip(),
    )


settings = _load()
