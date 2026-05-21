"""Environment-driven configuration for the example partner portal.

Read once at import; values are plain attributes on a dataclass so tests
can monkeypatch a fresh instance without an env-var dance.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    partner_brand: str
    mintoffice_api_url: str
    mintoffice_api_key: str
    mintoffice_webhook_secret: str
    mintoffice_timeout_seconds: float
    mintoffice_retries: int
    public_base_url: str
    admin_username: str
    admin_password: str
    admin_page_size: int
    db_path: str


def _int(value: str, default: int, *, lo: int = 0, hi: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return default
    if hi is not None and n > hi:
        return hi
    return n


def _float(value: str, default: float, *, lo: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n >= lo else default


def _load() -> Settings:
    return Settings(
        partner_brand=os.environ.get("PARTNER_BRAND", "ExampleAI").strip() or "ExampleAI",
        mintoffice_api_url=os.environ.get(
            "MINTOFFICE_API_URL", "https://mint.mintbot.dev"
        ).rstrip("/"),
        mintoffice_api_key=os.environ.get("MINTOFFICE_API_KEY", "").strip(),
        mintoffice_webhook_secret=os.environ.get(
            "MINTOFFICE_WEBHOOK_SECRET", ""
        ).strip(),
        mintoffice_timeout_seconds=_float(
            os.environ.get("MINTOFFICE_TIMEOUT_SECONDS", "15"), 15.0, lo=1.0,
        ),
        mintoffice_retries=_int(
            os.environ.get("MINTOFFICE_RETRIES", "2"), 2, lo=0, hi=5,
        ),
        public_base_url=os.environ.get(
            "PUBLIC_BASE_URL", "http://127.0.0.1:8000"
        ).rstrip("/"),
        admin_username=os.environ.get("ADMIN_USERNAME", "admin").strip(),
        admin_password=os.environ.get("ADMIN_PASSWORD", "").strip(),
        admin_page_size=_int(
            os.environ.get("ADMIN_PAGE_SIZE", "50"), 50, lo=1, hi=500,
        ),
        db_path=os.environ.get("DB_PATH", "portal.db").strip(),
    )


settings = _load()
