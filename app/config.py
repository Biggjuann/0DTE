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


def _zone_map(name: str, default: dict) -> dict:
    """Parse 'SPY:0.5,QQQ:0.75' into {ticker: zone_width}."""
    out = dict(default)
    raw = os.getenv(name, "")
    for part in raw.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip().upper()] = float(v)
            except ValueError:
                pass
    return out


@dataclass
class Settings:
    data_mode: str = field(default_factory=lambda: os.getenv("DATA_MODE", "mock").lower())
    tickers: List[str] = field(default_factory=lambda: _list("TICKERS", ["QQQ", "SPY"]))

    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    # ----- Regular-trading-hours gate (shared by both strategies) ------------
    # 0DTE options trade RTH only: no entries outside the session, and any open
    # position is flattened before the close. All times are US/Eastern.
    rth_only: bool = field(default_factory=lambda: _bool("RTH_ONLY", True))
    session_open: str = field(default_factory=lambda: os.getenv("SESSION_OPEN", "09:30"))
    session_close: str = field(default_factory=lambda: os.getenv("SESSION_CLOSE", "16:00"))
    session_no_entry: str = field(default_factory=lambda: os.getenv("SESSION_NO_ENTRY", "15:45"))
    session_flatten: str = field(default_factory=lambda: os.getenv("SESSION_FLATTEN", "15:55"))
    contracts: int = field(default_factory=lambda: _int("CONTRACTS", 1))
    proximity: float = field(default_factory=lambda: _float("PROXIMITY", 1.0))
    # 0DTE call strike = mid level + this offset (dollars). Spec: $1 above mid.
    strike_offset: float = field(default_factory=lambda: _float("STRIKE_OFFSET", 1.0))
    # Minimum separation (dollars) between lower->mid and mid->top. Default 0 =
    # only require proper ordering (lower < mid < top); raise to stand aside on
    # tight channels.
    min_channel_gap: float = field(default_factory=lambda: _float("MIN_CHANNEL_GAP", 0.0))
    # Arm a breakeven stop once open profit reaches this fraction of premium
    # (1.0 = +100% / premium doubled). 0 disables it.
    breakeven_arm_profit: float = field(default_factory=lambda: _float("BREAKEVEN_ARM_PROFIT", 1.0))
    # Anti-whipsaw: after a STOP, block re-entry on the same ticker for this long.
    stop_cooldown_seconds: int = field(default_factory=lambda: _int("STOP_COOLDOWN_SECONDS", 180))
    # Price must rise this far above the put wall to re-arm (0 = use proximity).
    rearm_distance: float = field(default_factory=lambda: _float("REARM_DISTANCE", 0.0))

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

    # ----- Person's Pivots strategy (second tab) -----------------------------
    pivot_contracts: int = field(default_factory=lambda: _int("PIVOT_CONTRACTS", 4))
    # Touch band for reaching R1 / S1 / PP / target (dollars) — fallback when a
    # ticker has no zone configured below.
    pivot_proximity: float = field(default_factory=lambda: _float("PIVOT_PROXIMITY", 0.5))
    # Each pivot level is a ZONE this many dollars wide, centered on the line.
    # A level is "reached" when price enters its zone (within width/2). Per ticker.
    pivot_zones: dict = field(default_factory=lambda: _zone_map("PIVOT_ZONES", {"SPY": 0.50, "QQQ": 0.75}))
    # Premium stop = this fraction of the entry premium lost. Default 0 = NO stop
    # (runners go pure zone-to-zone / EOD). Set >0 to re-enable + breakeven-on-scale.
    pivot_stop_pct: float = field(default_factory=lambda: _float("PIVOT_STOP_PCT", 0.0))
    # Scale the lot down to the runner once premium is up this fraction (0.5 = +50%).
    pivot_scale_profit: float = field(default_factory=lambda: _float("PIVOT_SCALE_PROFIT", 0.5))
    # Contracts left as the runner after scaling (rest are sold at +profit).
    pivot_runner_qty: int = field(default_factory=lambda: _int("PIVOT_RUNNER_CONTRACTS", 1))
    # (legacy) fraction scaled at the pivot — unused by the zone-fade logic.
    pivot_scale_pct: float = field(default_factory=lambda: _float("PIVOT_SCALE_PCT", 0.5))
    # VIX symbol used to compute the regime (its own daily pivot).
    vix_symbol: str = field(default_factory=lambda: os.getenv("VIX_SYMBOL", "$VIX"))
    # --- bad-data guards (a single spiked print must not trigger a trade) ---
    # Reject a decision price that deviates more than this from the prior close.
    pivot_max_dev_pct: float = field(default_factory=lambda: _float("PIVOT_MAX_DEV_PCT", 0.03))
    # Reject a decision price that jumps more than this between consecutive ticks.
    pivot_max_jump_pct: float = field(default_factory=lambda: _float("PIVOT_MAX_JUMP_PCT", 0.02))
    # Let a fresh position breathe before any scale/stop/target can fire (seconds).
    pivot_min_hold_seconds: float = field(default_factory=lambda: _float("PIVOT_MIN_HOLD_SECONDS", 3.0))
    pivot_trade_log_path: str = field(default_factory=lambda: os.getenv("PIVOT_TRADE_LOG_PATH", ""))

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

    def pivot_zone_width(self, ticker: str) -> float:
        """Full zone width (dollars) for a ticker; falls back to 2×proximity."""
        w = self.pivot_zones.get(ticker.upper())
        return w if w is not None else self.pivot_proximity * 2.0

    def pivot_zone_half(self, ticker: str) -> float:
        """Half-width: a level is reached when price is within this of the line."""
        return self.pivot_zone_width(ticker) / 2.0


settings = Settings()
