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

    def get_contract(self, symbol: str) -> Optional[OptionContract]: ...


class Broker(Protocol):
    """Order placement. Long-only is enforced here as a hard invariant."""

    def buy_to_open_call(self, contract: OptionContract, qty: int) -> "Fill": ...

    def sell_to_close_call(self, contract: OptionContract, qty: int) -> "Fill": ...


class Fill:
    """Result of an order. `accepted` is False if the broker rejected it."""

    def __init__(self, accepted: bool, price: float, message: str = "", order_id: str = ""):
        self.accepted = accepted
        self.price = price
        self.message = message
        self.order_id = order_id
