"""Person's pivots provider — computes daily pivots from prior-session OHLC.

PP = (H + L + C) / 3
R1 = 2*PP - L   R2 = PP + H - L   R3 = R2 + H - L
S1 = 2*PP - H   S2 = PP - H + L   S3 = S2 - H + L

The OHLC source must expose get_prior_day_ohlc(ticker) (Schwab in live, the
mock provider otherwise). Pivots are constant intraday, so the engine polls
this on a slow cadence.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.models import PivotLevels

log = logging.getLogger("pivots")


class PivotsProvider:
    def __init__(self, ohlc_source):
        self.ohlc = ohlc_source

    def get_pivots(self, ticker: str) -> Optional[PivotLevels]:
        o = self.ohlc.get_prior_day_ohlc(ticker)
        if not o:
            log.warning("pivots: no prior-day OHLC for %s", ticker)
            return None
        return PivotLevels.from_ohlc(ticker, o["high"], o["low"], o["close"])
