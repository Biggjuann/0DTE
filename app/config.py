"""Runtime configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    data_mode: str = field(default_factory=lambda: os.getenv("DATA_MODE", "mock").lower())
    tickers: List[str] = field(default_factory=lambda: _list("TICKERS", ["QQQ", "SPY"]))

    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    contracts: int = field(default_factory=lambda: _int("CONTRACTS", 1))
    proximity: float = field(default_factory=lambda: _float("PROXIMITY", 1.0))

    quote_poll_seconds: int = field(default_factory=lambda: _int("QUOTE_POLL_SECONDS", 5))
    levels_poll_seconds: int = field(default_factory=lambda: _int("LEVELS_POLL_SECONDS", 60))
    mm_poll_seconds: int = field(default_factory=lambda: _int("MM_POLL_SECONDS", 15))

    mm_base_url: str = field(default_factory=lambda: os.getenv("MM_BASE_URL", "https://web-production-fff5c.up.railway.app"))
    mm_api_key: str = field(default_factory=lambda: os.getenv("MM_API_KEY", ""))

    gamma_base_url: str = field(default_factory=lambda: os.getenv("GAMMA_BASE_URL", "https://gammagamma-production.up.railway.app"))
    gamma_api_key: str = field(default_factory=lambda: os.getenv("GAMMA_API_KEY", ""))
    gamma_expiry: str = field(default_factory=lambda: os.getenv("GAMMA_EXPIRY", "weekly"))

    schwab_base_url: str = field(default_factory=lambda: os.getenv("SCHWAB_BASE_URL", "https://api.schwabapi.com"))
    schwab_account_hash: str = field(default_factory=lambda: os.getenv("SCHWAB_ACCOUNT_HASH", ""))

    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))

    @property
    def live(self) -> bool:
        return self.data_mode == "live"


settings = Settings()
