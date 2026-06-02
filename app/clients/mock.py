"""Self-contained synthetic feed.

Drives the full strategy lifecycle (DISABLED -> ARMED -> OPEN -> exit) on a
loop so the dashboard is fully alive with zero external dependencies. Each
ticker walks price from near its lower level up toward its top level while the
MM status starts LONG and downgrades to CAUTIOUS_LONG near the mid, exercising
both the mid-exit and top-exit paths.
"""
from __future__ import annotations

import math
import random
import time
from typing import Dict, List, Optional

from app.clients.base import Fill
from app.models import Levels, MMSignal, MMStatus, OptionContract, Quote

# Anchor levels roughly matching the example dashboards.
_BASE: Dict[str, Dict[str, float]] = {
    "QQQ": {"lower": 525.0, "mid": 532.0, "top": 540.0},
    "SPY": {"lower": 588.0, "mid": 594.0, "top": 600.0},
}


def _anchor(ticker: str) -> Dict[str, float]:
    if ticker in _BASE:
        return _BASE[ticker]
    # Deterministic pseudo-levels for any other symbol.
    seed = sum(ord(c) for c in ticker)
    lower = 100 + seed % 300
    return {"lower": float(lower), "mid": float(lower + 7), "top": float(lower + 15)}


class MockProvider:
    """Implements LevelsProvider + MMProvider + MarketData + Broker."""

    def __init__(self, period_seconds: float = 240.0):
        self.t0 = time.time()
        self.period = period_seconds
        self._orders = 0

    # --- price model: triangular sweep lower -> top -> lower ----------------
    def _phase(self, ticker: str) -> float:
        offset = (sum(ord(c) for c in ticker) % 60)
        return ((time.time() - self.t0 + offset) % self.period) / self.period

    def _price(self, ticker: str) -> float:
        a = _anchor(ticker)
        p = self._phase(ticker)
        # 0..0.5 climb lower->top, 0.5..1 drift back down.
        if p < 0.5:
            base = a["lower"] + (a["top"] - a["lower"]) * (p / 0.5)
        else:
            base = a["top"] - (a["top"] - a["lower"]) * ((p - 0.5) / 0.5)
        jitter = math.sin(time.time() / 3.0 + len(ticker)) * 0.25
        return round(base + jitter, 2)

    # --- LevelsProvider ------------------------------------------------------
    def get_levels(self, ticker: str) -> Optional[Levels]:
        a = _anchor(ticker)
        spot = self._price(ticker)
        return Levels(
            ticker=ticker,
            lower=a["lower"],
            mid=a["mid"],
            top=a["top"],
            gvwap=a["mid"],
            gamma_flip=a["mid"] + 1,
            lowest_put=a["lower"],
            highest_call=a["top"],
            spot=spot,
            expiry="weekly",
        )

    # --- MMProvider ----------------------------------------------------------
    def get_signal(self, ticker: str) -> Optional[MMSignal]:
        a = _anchor(ticker)
        price = self._price(ticker)
        span = a["top"] - a["lower"]
        progress = (price - a["lower"]) / span if span else 0.0
        # LONG while climbing through the lower half; downgrade to CAUTIOUS_LONG
        # once price is at/above the mid (so the mid-exit can trigger).
        status = MMStatus.LONG if price < a["mid"] else MMStatus.CAUTIOUS_LONG
        puts_below = 1_400_000 * (1.1 - progress)
        calls_above = 900_000 * (0.9 + progress)
        reason = (
            "BULLS in control — puts below spot dominate calls at/above spot."
            if status is MMStatus.LONG
            else "Momentum cooling — calls hedging picking up near the mid."
        )
        return MMSignal(
            ticker=ticker,
            status=status,
            puts_below_spot=round(puts_below),
            puts_at_above_spot=round(700_000 * (0.8 + progress)),
            calls_below_spot=round(600_000 * (1.0 - progress * 0.5)),
            calls_at_above_spot=round(calls_above),
            reasoning=[reason],
        )

    # --- MarketData ----------------------------------------------------------
    def get_quote(self, ticker: str) -> Optional[Quote]:
        price = self._price(ticker)
        return Quote(ticker=ticker, last=price, minute_close=price)

    def _premium(self, underlying: float, strike: float) -> float:
        # Crude intrinsic + time value so P&L moves with the underlying.
        intrinsic = max(0.0, underlying - strike)
        return round(intrinsic + 1.25, 2)

    def get_0dte_calls(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]:
        under = self._price(ticker)
        out: List[OptionContract] = []
        base = round(near_strike)
        for k in range(int(base - width), int(base + width) + 1):
            prem = self._premium(under, k)
            out.append(
                OptionContract(
                    symbol=f"{ticker}_0DTE_C{k}",
                    strike=float(k),
                    expiry="0dte",
                    bid=round(prem - 0.05, 2),
                    ask=round(prem + 0.05, 2),
                    last=prem,
                )
            )
        return out

    def get_contract(self, symbol: str) -> Optional[OptionContract]:
        try:
            ticker = symbol.split("_")[0]
            strike = float(symbol.split("C")[-1])
        except Exception:
            return None
        under = self._price(ticker)
        prem = self._premium(under, strike)
        return OptionContract(symbol=symbol, strike=strike, expiry="0dte",
                              bid=round(prem - 0.05, 2), ask=round(prem + 0.05, 2), last=prem)

    # --- Broker --------------------------------------------------------------
    def buy_to_open_call(self, contract: OptionContract, qty: int) -> Fill:
        self._orders += 1
        return Fill(True, contract.ask or contract.mid, "mock fill", f"mock-{self._orders}")

    def sell_to_close_call(self, contract: OptionContract, qty: int) -> Fill:
        self._orders += 1
        return Fill(True, contract.bid or contract.mid, "mock fill", f"mock-{self._orders}")
