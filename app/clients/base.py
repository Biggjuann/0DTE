"""Adapter interfaces.

The strategy engine only ever talks to these four protocols, so swapping the
mock feed for the real MM / Gammagamma / Schwab services is a config change,
never a code change.
"""
from __future__ import annotations

from typing import List, Optional, Protocol

from app.models import Levels, MMSignal, OptionContract, Quote


class LevelsProvider(Protocol):
    """Gammagamma: structural levels for the weekly expiry."""

    def get_levels(self, ticker: str) -> Optional[Levels]: ...


class MMProvider(Protocol):
    """MM: trend status + puts/calls flow."""

    def get_signal(self, ticker: str) -> Optional[MMSignal]: ...


class MarketData(Protocol):
    """Quotes (1-minute closes) + 0DTE option chain. Backed by the Schwab token."""

    def get_quote(self, ticker: str) -> Optional[Quote]: ...

    def get_0dte_calls(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]: ...

    def get_0dte_puts(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]: ...

    def get_contract(self, symbol: str) -> Optional[OptionContract]: ...

    def get_prior_ohlc(self, ticker: str, timeframe: str = "weekly") -> Optional[dict]:
        """{'high','low','close'} of the prior completed period (for pivots).

        timeframe: "weekly" (prior completed week) or "daily" (prior session)."""
        ...

    def get_prior_day_ohlc(self, ticker: str) -> Optional[dict]:
        """Back-compat alias for get_prior_ohlc(ticker, 'daily')."""
        ...


class Broker(Protocol):
    """Order placement. Buying options only (calls or puts) — never sell-to-open."""

    def buy_to_open(self, contract: OptionContract, qty: int) -> "Fill": ...

    def sell_to_close(self, contract: OptionContract, qty: int) -> "Fill": ...

    # Strategy-1 call-only aliases (kept for compatibility).
    def buy_to_open_call(self, contract: OptionContract, qty: int) -> "Fill": ...

    def sell_to_close_call(self, contract: OptionContract, qty: int) -> "Fill": ...


class Fill:
    """Result of an order. `accepted` is False if the broker rejected it."""

    def __init__(self, accepted: bool, price: float, message: str = "", order_id: str = ""):
        self.accepted = accepted
        self.price = price
        self.message = message
        self.order_id = order_id
