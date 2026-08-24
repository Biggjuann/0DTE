"""Person's pivots provider — computes pivots from the prior period's OHLC.

PP = (H + L + C) / 3
R1 = 2*PP - L   R2 = PP + H - L   R3 = R2 + H - L
S1 = 2*PP - H   S2 = PP - H + L   S3 = S2 - H + L

Timeframe (PIVOT_TIMEFRAME, default "weekly") selects the prior period: weekly
uses the prior completed WEEK's OHLC, daily the prior session's. The OHLC source
exposes get_prior_ohlc(ticker, timeframe) (Schwab in live, the mock otherwise).
Pivots are constant within their period, so we compute them once on the first
fetch of the (US/Eastern) day and FREEZE them — the live prior candle can be
revised slightly, and we never want the plan levels drifting under an open trade.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from app import market_hours
from app.config import settings
from app.models import PivotLevels

log = logging.getLogger("pivots")


class PivotsProvider:
    def __init__(self, ohlc_source, timeframe: Optional[str] = None):
        self.ohlc = ohlc_source
        self.timeframe = (timeframe or settings.pivot_timeframe or "weekly").lower()
        # ticker -> (et_date_iso, frozen levels)
        self._cache: Dict[str, Tuple[str, PivotLevels]] = {}

    def _fetch(self, ticker: str):
        # Prefer the timeframe-aware API; fall back to the daily-only method.
        fn = getattr(self.ohlc, "get_prior_ohlc", None)
        if fn is not None:
            return fn(ticker, self.timeframe)
        return self.ohlc.get_prior_day_ohlc(ticker)

    def get_pivots(self, ticker: str) -> Optional[PivotLevels]:
        today = market_hours.now_et().date().isoformat()
        cached = self._cache.get(ticker)
        if cached and cached[0] == today:
            return cached[1]                      # frozen for the session
        o = self._fetch(ticker)
        if not o:
            log.warning("pivots: no prior-%s OHLC for %s", self.timeframe, ticker)
            return cached[1] if cached else None  # serve stale rather than nothing
        pv = PivotLevels.from_ohlc(ticker, o["high"], o["low"], o["close"])
        self._cache[ticker] = (today, pv)
        log.info("pivots frozen (%s) for %s %s: PP=%.2f R1=%.2f S1=%.2f",
                 self.timeframe, ticker, today, pv.pp, pv.r1, pv.s1)
        return pv
