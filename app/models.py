"""Domain models shared across the strategy, clients and API layer."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional


class MMStatus(str, Enum):
    """Trend status published by the MM service.

    Only LONG / CAUTIOUS_LONG grant entry approval for this long-only system.
    Anything else is treated as "no approval" (and forces a protective exit
    if we are already in a position).
    """

    LONG = "long"
    CAUTIOUS_LONG = "cautious_long"
    NEUTRAL = "neutral"
    CAUTIOUS_SHORT = "cautious_short"
    SHORT = "short"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: object) -> "MMStatus":
        if isinstance(raw, MMStatus):
            return raw
        s = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
        mapping = {
            "long": cls.LONG,
            "long_bias": cls.LONG,
            "bullish": cls.LONG,
            "cautious_long": cls.CAUTIOUS_LONG,
            "cautiously_long": cls.CAUTIOUS_LONG,
            "neutral": cls.NEUTRAL,
            "flat": cls.NEUTRAL,
            "cautious_short": cls.CAUTIOUS_SHORT,
            "short": cls.SHORT,
            "bearish": cls.SHORT,
        }
        return mapping.get(s, cls.UNKNOWN)

    @property
    def approves_entry(self) -> bool:
        return self in (MMStatus.LONG, MMStatus.CAUTIOUS_LONG)


class PositionState(str, Enum):
    DISABLED = "disabled"   # MM status does not approve trading
    ARMED = "armed"         # approved + watching for the entry trigger
    OPEN = "open"           # holding a long 0DTE call
    FLAT = "flat"           # approved-but-idle baseline (no trigger yet)


@dataclass
class Levels:
    """The three structural levels derived from Gammagamma (weekly)."""

    ticker: str
    lower: float            # lowest put wall on the weekly
    mid: float              # gvwap (preferred) or gamma flip
    top: float              # highest call wall on the weekly
    gvwap: Optional[float] = None
    gamma_flip: Optional[float] = None
    lowest_put: Optional[float] = None
    highest_call: Optional[float] = None
    spot: Optional[float] = None
    expiry: Optional[str] = None
    asof: float = field(default_factory=time.time)


@dataclass
class MMSignal:
    """Trend + flow snapshot from the MM service."""

    ticker: str
    status: MMStatus = MMStatus.UNKNOWN
    puts_below_spot: float = 0.0
    puts_at_above_spot: float = 0.0
    calls_below_spot: float = 0.0
    calls_at_above_spot: float = 0.0
    reasoning: List[str] = field(default_factory=list)
    asof: float = field(default_factory=time.time)

    @property
    def bull_control(self) -> bool:
        """Bulls in control: puts below spot dominate calls at/above spot.

        Mirrors the MM dashboard's "BULLS in control" confirmation line and is
        used as the flow confirmation gate on entries.
        """
        return self.puts_below_spot > self.calls_at_above_spot


@dataclass
class Quote:
    ticker: str
    last: float
    minute_close: float          # close of the most recent completed 1-minute bar
    asof: float = field(default_factory=time.time)


@dataclass
class OptionContract:
    symbol: str
    strike: float
    expiry: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2, 2)
        return self.last or self.ask or self.bid


@dataclass
class Position:
    ticker: str
    contract_symbol: str
    strike: float
    expiry: str
    qty: int
    entry_price: float           # per-contract premium paid
    entry_time: float
    entry_underlying: float
    current_price: float = 0.0   # current per-contract premium
    last_underlying: float = 0.0
    breakeven_armed: bool = False  # set once +100% profit is reached
    # Entry context (for review/analytics).
    entry_lower: Optional[float] = None
    entry_mid: Optional[float] = None
    entry_top: Optional[float] = None
    entry_stance: Optional[str] = None
    entry_bull_control: Optional[bool] = None
    # Excursions since entry (premium, per contract).
    max_premium: float = 0.0
    min_premium: float = 0.0
    max_underlying: float = 0.0
    min_underlying: float = 0.0

    def update_excursions(self) -> None:
        self.max_premium = max(self.max_premium, self.current_price)
        self.min_premium = min(self.min_premium, self.current_price) if self.min_premium else self.current_price
        if self.last_underlying:
            self.max_underlying = max(self.max_underlying, self.last_underlying)
            self.min_underlying = (min(self.min_underlying, self.last_underlying)
                                   if self.min_underlying else self.last_underlying)

    @property
    def mfe(self) -> float:
        """Max favorable excursion in dollars (best unrealised P&L seen)."""
        return round((self.max_premium - self.entry_price) * 100 * self.qty, 2)

    @property
    def mae(self) -> float:
        """Max adverse excursion in dollars (worst unrealised P&L seen)."""
        return round((self.min_premium - self.entry_price) * 100 * self.qty, 2)

    @property
    def pnl(self) -> float:
        return round((self.current_price - self.entry_price) * 100 * self.qty, 2)

    @property
    def pnl_pct(self) -> float:
        if not self.entry_price:
            return 0.0
        return round((self.current_price - self.entry_price) / self.entry_price * 100, 2)


@dataclass
class PivotLevels:
    """Person's pivots from the prior session's OHLC (higher timeframe = daily)."""

    ticker: str
    pp: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    prior_high: Optional[float] = None
    prior_low: Optional[float] = None
    prior_close: Optional[float] = None
    spot: Optional[float] = None
    asof: float = field(default_factory=time.time)

    @staticmethod
    def from_ohlc(ticker: str, high: float, low: float, close: float,
                  spot: Optional[float] = None) -> "PivotLevels":
        pp = (high + low + close) / 3.0
        r1 = 2 * pp - low
        r2 = pp + high - low
        r3 = r2 + high - low
        s1 = 2 * pp - high
        s2 = pp - high + low
        s3 = s2 - high + low
        return PivotLevels(ticker=ticker, pp=round(pp, 2), r1=round(r1, 2), r2=round(r2, 2),
                           r3=round(r3, 2), s1=round(s1, 2), s2=round(s2, 2), s3=round(s3, 2),
                           prior_high=high, prior_low=low, prior_close=close, spot=spot)


@dataclass
class PivotPosition:
    """A zone-fade trade: 4 ATM contracts, scale most at +50% profit, run a
    runner to the next zone (down for shorts, up for longs)."""

    ticker: str
    direction: str               # LONG / SHORT
    option_type: str             # CALL / PUT
    contract_symbol: str
    strike: float
    expiry: str
    qty: int                     # initial lot
    remaining_qty: int
    entry_price: float           # premium paid
    entry_time: float
    entry_underlying: float
    pp: float                    # the zone level we faded (entry zone)
    target: Optional[float] = None   # runner exit level (next zone), None if none
    entry_zone_label: str = ""       # e.g. "R1"
    target_label: str = ""           # e.g. "PP"
    current_price: float = 0.0
    last_underlying: float = 0.0
    scaled: bool = False         # the 3 lots taken at +50%
    breakeven: bool = False      # stop moved to breakeven
    stop_premium: float = 0.0    # premium stop level (<0 = disabled)
    realized_pnl: float = 0.0    # locked in from the scale-out

    @property
    def open_pnl(self) -> float:
        return round((self.current_price - self.entry_price) * 100 * self.remaining_qty, 2)

    @property
    def total_pnl(self) -> float:
        return round(self.realized_pnl + self.open_pnl, 2)


@dataclass
class TradeRecord:
    ts: float
    ticker: str
    action: str                  # ENTRY / EXIT
    reason: str
    underlying: float
    contract_symbol: str
    strike: float
    qty: int
    price: float                 # premium
    pnl: Optional[float] = None
    dry_run: bool = True
    # Context / analytics (EXIT records carry the full round-trip).
    stance: Optional[str] = None         # MM stance at this event
    bull_control: Optional[bool] = None
    lower: Optional[float] = None
    mid: Optional[float] = None
    top: Optional[float] = None
    exit_type: Optional[str] = None      # TOP / MID / STOP / PROTECTIVE / KILL
    entry_price: Optional[float] = None
    entry_underlying: Optional[float] = None
    entry_stance: Optional[str] = None
    hold_seconds: Optional[float] = None
    mae: Optional[float] = None          # max adverse excursion ($)
    mfe: Optional[float] = None          # max favorable excursion ($)


# Stable column order for CSV export.
TRADE_CSV_FIELDS = [
    "ts", "datetime", "ticker", "action", "exit_type", "reason",
    "underlying", "contract_symbol", "strike", "qty", "price", "pnl",
    "stance", "entry_stance", "bull_control", "lower", "mid", "top",
    "entry_price", "entry_underlying", "hold_seconds", "mae", "mfe", "dry_run",
]


def to_jsonable(obj):
    """Recursively convert dataclasses / enums to JSON-serialisable structures."""
    if isinstance(obj, Enum):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
