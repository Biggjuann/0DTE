"""Gammagamma-backed market data (quotes + 0DTE option pricing).

Used when MARKET_DATA_PROVIDER / OPTIONS_PROVIDER == "gammagamma". Gammagamma's
/api/ticker/{symbol} already carries per-strike 0DTE rows (strike, expiry, type,
mid), so it can price the option even when Schwab market-data entitlements are
unavailable. We build a Schwab OSI symbol from (ticker, expiry, strike) so the
contract remains routable through the Schwab broker.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import List, Optional

import httpx

from app.clients.gammagamma import DEFAULT_BACKEND
from app.config import settings
from app.models import OptionContract, Quote

log = logging.getLogger("gammamarket")


def osi_symbol(ticker: str, expiry_iso: str, strike: float, cp: str = "C") -> str:
    """Schwab/OCC OSI: 6-char root (space padded) + YYMMDD + C/P + strike*1000 (8d)."""
    try:
        d = dt.datetime.fromisoformat(expiry_iso.replace("Z", "+00:00")).date()
    except Exception:
        d = dt.date.today()
    return f"{ticker:<6}{d.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"


class GammaMarketData:
    def __init__(self, base_url: Optional[str] = None, expiry: Optional[str] = None, timeout: float = 10.0):
        base = (base_url or settings.gamma_base_url or DEFAULT_BACKEND).rstrip("/")
        if "divine-celebration" in base:
            base = DEFAULT_BACKEND
        self.base = base
        self.expiry = expiry or settings.gamma_expiry
        self.client = httpx.Client(timeout=timeout)

    def get_quote(self, ticker: str) -> Optional[Quote]:
        # Gammagamma has no 1-minute bars; spot is used as the close proxy.
        try:
            params = {"expiry": self.expiry} if self.expiry and self.expiry != "all" else {}
            r = self.client.get(f"{self.base}/api/levels/{ticker}", params=params)
            if r.status_code == 200 and r.text.strip():
                spot = r.json().get("spot")
                if spot is not None:
                    return Quote(ticker=ticker, last=float(spot), minute_close=float(spot))
        except Exception as exc:  # pragma: no cover - network
            log.debug("gamma quote %s failed: %s", ticker, exc)
        return None

    def _rows(self, ticker: str) -> List[dict]:
        try:
            r = self.client.get(f"{self.base}/api/ticker/{ticker}")
            if r.status_code == 200:
                return r.json().get("rows") or []
        except Exception as exc:  # pragma: no cover - network
            log.debug("gamma rows %s failed: %s", ticker, exc)
        return []

    def get_0dte_calls(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]:
        out: List[OptionContract] = []
        for row in self._rows(ticker):
            if row.get("type") != "C" or float(row.get("dte", 99)) >= 1.0:
                continue
            strike = float(row["strike"])
            if abs(strike - near_strike) > width:
                continue
            mid = float(row.get("mid") or 0)
            out.append(OptionContract(
                symbol=osi_symbol(ticker, str(row.get("expiry", "")), strike, "C"),
                strike=strike, expiry=str(row.get("expiry", "0dte")),
                bid=round(mid - 0.05, 2), ask=round(mid + 0.05, 2), last=mid,
            ))
        return out

    def get_contract(self, symbol: str) -> Optional[OptionContract]:
        # OSI: root(6) + YYMMDD(6) + C/P(1) + strike*1000(8)
        try:
            ticker = symbol[:6].strip()
            strike = int(symbol[-8:]) / 1000.0
        except Exception:
            return None
        for row in self._rows(ticker):
            if row.get("type") == "C" and abs(float(row["strike"]) - strike) < 0.01:
                mid = float(row.get("mid") or 0)
                return OptionContract(symbol=symbol, strike=strike, expiry=str(row.get("expiry", "0dte")),
                                      bid=round(mid - 0.05, 2), ask=round(mid + 0.05, 2), last=mid)
        return None


class CompositeMarketData:
    """Routes quotes to one source and options to another."""

    def __init__(self, quote_src, option_src):
        self.quote_src = quote_src
        self.option_src = option_src

    @property
    def last_error(self):
        return getattr(self.quote_src, "last_error", None)

    def get_quote(self, ticker: str):
        return self.quote_src.get_quote(ticker)

    def get_0dte_calls(self, ticker: str, near_strike: float, width: float = 5.0):
        return self.option_src.get_0dte_calls(ticker, near_strike, width)

    def get_contract(self, symbol: str):
        return self.option_src.get_contract(symbol)
