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


def _is_vix(ticker: str) -> bool:
    return str(ticker).upper().lstrip("$").startswith("VIX")


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
        # Sweep floor sits just inside the entry band ($0.5 above the lower
        # level) so the trigger fires cleanly without the jitter tripping the
        # "close below lower" stop. 0..0.5 climb floor->top, 0.5..1 back down.
        floor = a["lower"] + 0.5
        if p < 0.5:
            base = floor + (a["top"] - floor) * (p / 0.5)
        else:
            base = a["top"] - (a["top"] - floor) * ((p - 0.5) / 0.5)
        jitter = math.sin(time.time() / 3.0 + len(ticker)) * 0.2
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
        # Stay LONG for the whole CLIMB (lower -> top) so an open position rides
        # all the way to the TOP call wall and exits there (the primary path).
        # Downgrade to CAUTIOUS_LONG on the way back DOWN; entries are still
        # approved at the bottom, then flip to LONG again on the next climb.
        climbing = self._phase(ticker) < 0.5
        status = MMStatus.LONG if climbing else MMStatus.CAUTIOUS_LONG
        puts_below = 1_400_000 * (1.1 - progress)
        calls_above = 900_000 * (0.9 + progress)
        reason = (
            "BULLS in control — puts below spot dominate calls ≥ spot. Ride it."
            if status is MMStatus.LONG
            else "Momentum cooling on the pullback — cautious long."
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
    def _vix(self) -> float:
        # Oscillates around the VIX pivot (20) so the regime flips over time.
        return round(20 + 4 * math.sin(time.time() / 40.0), 2)

    def get_quote(self, ticker: str) -> Optional[Quote]:
        if _is_vix(ticker):
            v = self._vix()
            return Quote(ticker=ticker, last=v, minute_close=v)
        price = self._price(ticker)
        return Quote(ticker=ticker, last=price, minute_close=price)

    def _premium(self, underlying: float, strike: float, cp: str = "C") -> float:
        # Crude intrinsic + time value so P&L moves with the underlying.
        intrinsic = max(0.0, underlying - strike) if cp == "C" else max(0.0, strike - underlying)
        return round(intrinsic + 1.25, 2)

    def _chain(self, ticker: str, near_strike: float, width: float, cp: str) -> List[OptionContract]:
        under = self._price(ticker)
        out: List[OptionContract] = []
        base = round(near_strike)
        for k in range(int(base - width), int(base + width) + 1):
            prem = self._premium(under, k, cp)
            out.append(OptionContract(symbol=f"{ticker}_0DTE_{cp}{k}", strike=float(k),
                                      expiry="0dte", bid=round(prem - 0.05, 2),
                                      ask=round(prem + 0.05, 2), last=prem))
        return out

    def get_0dte_calls(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]:
        return self._chain(ticker, near_strike, width, "C")

    def get_0dte_puts(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]:
        return self._chain(ticker, near_strike, width, "P")

    def get_contract(self, symbol: str) -> Optional[OptionContract]:
        try:
            ticker = symbol.split("_")[0]
            cp = "P" if "_0DTE_P" in symbol else "C"
            strike = float(symbol.split(cp)[-1])
        except Exception:
            return None
        under = self._price(ticker)
        prem = self._premium(under, strike, cp)
        return OptionContract(symbol=symbol, strike=strike, expiry="0dte",
                              bid=round(prem - 0.05, 2), ask=round(prem + 0.05, 2), last=prem)

    # --- prior-day OHLC (for pivots) -----------------------------------------
    def get_prior_day_ohlc(self, ticker: str) -> Optional[dict]:
        if _is_vix(ticker):
            return {"high": 22.0, "low": 18.0, "close": 20.0}  # VIX PP = 20
        a = _anchor(ticker)
        # Place pivots inside the price sweep: PP~mid, R1 near top, S1 near floor.
        mid, lo, top = a["mid"], a["lower"], a["top"]
        high = round(mid + (top - mid) * 0.6, 2)
        low = round(mid - (mid - lo) * 0.6, 2)
        close = round(mid + 1, 2)
        return {"high": high, "low": low, "close": close}

    # --- Broker --------------------------------------------------------------
    def buy_to_open(self, contract: OptionContract, qty: int) -> Fill:
        self._orders += 1
        return Fill(True, contract.ask or contract.mid, "mock fill", f"mock-{self._orders}")

    def sell_to_close(self, contract: OptionContract, qty: int) -> Fill:
        self._orders += 1
        return Fill(True, contract.bid or contract.mid, "mock fill", f"mock-{self._orders}")

    buy_to_open_call = buy_to_open
    sell_to_close_call = sell_to_close
