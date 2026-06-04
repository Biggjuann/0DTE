"""Person's pivots provider — computes daily pivots from prior-session OHLC.

PP = (H + L + C) / 3
R1 = 2*PP - L   R2 = PP + H - L   R3 = R2 + H - L
S1 = 2*PP - H   S2 = PP - H + L   S3 = S2 - H + L

The OHLC source must expose get_prior_day_ohlc(ticker) (Schwab in live, the
mock provider otherwise). Pivots are constant for a trading day, so we compute
them once on the first fetch of the (US/Eastern) day and FREEZE them — the live
prior-day candle can be revised slightly intraday, and we never want the day's
plan levels (scale / target / stop reference) drifting under an open trade.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from app import market_hours
from app.models import PivotLevels

log = logging.getLogger("pivots")


class PivotsProvider:
    def __init__(self, ohlc_source):
        self.ohlc = ohlc_source
        # ticker -> (et_date_iso, frozen levels)
        self._cache: Dict[str, Tuple[str, PivotLevels]] = {}

    def get_pivots(self, ticker: str) -> Optional[PivotLevels]:
        today = market_hours.now_et().date().isoformat()
        cached = self._cache.get(ticker)
        if cached and cached[0] == today:
            return cached[1]                      # frozen for the session
        o = self.ohlc.get_prior_day_ohlc(ticker)
        if not o:
            log.warning("pivots: no prior-day OHLC for %s", ticker)
            return cached[1] if cached else None  # serve stale rather than nothing
        pv = PivotLevels.from_ohlc(ticker, o["high"], o["low"], o["close"])
        self._cache[ticker] = (today, pv)
        log.info("pivots frozen for %s %s: PP=%.2f R1=%.2f S1=%.2f", ticker, today,
                 pv.pp, pv.r1, pv.s1)
        return pv
