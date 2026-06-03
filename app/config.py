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
    # 0DTE call strike = mid level + this offset (dollars). Spec: $1 above mid.
    strike_offset: float = field(default_factory=lambda: _float("STRIKE_OFFSET", 1.0))

    quote_poll_seconds: int = field(default_factory=lambda: _int("QUOTE_POLL_SECONDS", 5))
    levels_poll_seconds: int = field(default_factory=lambda: _int("LEVELS_POLL_SECONDS", 60))
    mm_poll_seconds: int = field(default_factory=lambda: _int("MM_POLL_SECONDS", 15))

    mm_base_url: str = field(default_factory=lambda: os.getenv("MM_BASE_URL", "https://web-production-fff5c.up.railway.app"))
    mm_api_key: str = field(default_factory=lambda: os.getenv("MM_API_KEY", ""))

    gamma_base_url: str = field(default_factory=lambda: os.getenv("GAMMA_BASE_URL", "https://gammagamma-production.up.railway.app"))
    gamma_api_key: str = field(default_factory=lambda: os.getenv("GAMMA_API_KEY", ""))
    gamma_expiry: str = field(default_factory=lambda: os.getenv("GAMMA_EXPIRY", "weekly,0dte"))

    schwab_base_url: str = field(default_factory=lambda: os.getenv("SCHWAB_BASE_URL", "https://api.schwabapi.com"))
    schwab_account_hash: str = field(default_factory=lambda: os.getenv("SCHWAB_ACCOUNT_HASH", ""))

    # Shared-token mechanism (matches the MM service's variables).
    # SCHWAB_AUTH_MODE: "shared" pulls the access token from SCHWAB_TOKEN_URL
    # using SCHWAB_TOKEN_SHARE_KEY as the bearer.
    schwab_auth_mode: str = field(default_factory=lambda: os.getenv("SCHWAB_AUTH_MODE", "shared").lower())
    schwab_token_url: str = field(default_factory=lambda: os.getenv("SCHWAB_TOKEN_URL", ""))
    schwab_token_share_key: str = field(default_factory=lambda: os.getenv("SCHWAB_TOKEN_SHARE_KEY", ""))
    # Which backend supplies underlying quotes / option chains: "schwab" or
    # "gammagamma". Orders always route through Schwab regardless.
    market_data_provider: str = field(default_factory=lambda: os.getenv("MARKET_DATA_PROVIDER", "schwab").lower())
    options_provider: str = field(default_factory=lambda: os.getenv("OPTIONS_PROVIDER", "schwab").lower())

    # Trade log JSON path. Point this at a mounted Railway Volume (e.g.
    # /data/trades.json) so the log survives redeploys. Blank -> app/data/trades.json.
    trade_log_path: str = field(default_factory=lambda: os.getenv("TRADE_LOG_PATH", ""))

    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))

    @property
    def live(self) -> bool:
        return self.data_mode == "live"

    @property
    def token_url(self) -> str:
        """Resolved Schwab token endpoint (defaults to MM's /auth/token)."""
        return self.schwab_token_url or f"{self.mm_base_url.rstrip('/')}/auth/token"

    @property
    def token_share_key(self) -> str:
        """Bearer key for the shared token (SCHWAB_TOKEN_SHARE_KEY, MM_API_KEY fallback)."""
        return self.schwab_token_share_key or self.mm_api_key


settings = Settings()
