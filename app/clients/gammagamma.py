"""Live levels provider — Gammagamma backend.

Verified contract (https://gammagamma-production.up.railway.app):

    GET /api/levels/{symbol}?expiry=weekly
    {
      "underlying":"SPY","spot":756.7,"expiry_filter":"weekly",
      "call_wall":760.0,"put_wall":755.0,"gamma_flip":759.0,"gvwap":756.66,
      "major_call_walls":[760.0,755.0,758.0],
      "major_put_walls":[755.0,757.0,756.0]
    }

Level derivation (per the strategy spec):
    lower = lowest put wall on the weekly   -> min(major_put_walls)  (fallback put_wall)
    top   = largest call strike on the weekly-> max(major_call_walls) (fallback call_wall)
    mid   = gvwap (preferred) else gamma_flip

Weekly data can be empty intraday/after-hours; we then fall back to the
default (all-expiry) snapshot so the system still has levels to work with.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from app.config import settings
from app.models import Levels

log = logging.getLogger("gammagamma")

# The Next.js dashboard at divine-celebration talks to this backend; if the
# user points GAMMA_BASE_URL at the frontend we transparently redirect here.
DEFAULT_BACKEND = "https://gammagamma-production.up.railway.app"


def _num(*vals) -> Optional[float]:
    for v in vals:
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _strikes(walls) -> List[float]:
    out: List[float] = []
    for w in walls or []:
        n = _num(w if not isinstance(w, dict) else w.get("strike", w.get("level")))
        if n is not None:
            out.append(n)
    return out


class GammaGammaProvider:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 expiry: Optional[str] = None, timeout: float = 8.0):
        base = (base_url or settings.gamma_base_url or DEFAULT_BACKEND).rstrip("/")
        # If a frontend domain was supplied, swap to the API backend.
        if "divine-celebration" in base:
            base = DEFAULT_BACKEND
        self.base = base
        self.expiry = expiry or settings.gamma_expiry
        headers = {}
        if api_key or settings.gamma_api_key:
            headers["Authorization"] = f"Bearer {api_key or settings.gamma_api_key}"
        self.client = httpx.Client(timeout=timeout, headers=headers)

    def _get(self, symbol: str, expiry: Optional[str]) -> Optional[dict]:
        params = {}
        if expiry and expiry != "all":
            params["expiry"] = expiry
        try:
            r = self.client.get(f"{self.base}/api/levels/{symbol}", params=params)
            if r.status_code == 200 and r.text.strip():
                return r.json()
        except Exception as exc:  # pragma: no cover - network
            log.debug("gamma fetch %s failed: %s", symbol, exc)
        return None

    def get_levels(self, ticker: str) -> Optional[Levels]:
        data = self._get(ticker, self.expiry)
        # Weekly snapshot can be empty after-hours -> fall back to all-expiry.
        if not data and self.expiry != "all":
            data = self._get(ticker, "all")
        if not data:
            log.warning("Gammagamma: no levels for %s", ticker)
            return None

        gvwap = _num(data.get("gvwap"))
        gamma_flip = _num(data.get("gamma_flip"))
        spot = _num(data.get("spot"))

        call_walls = _strikes(data.get("major_call_walls"))
        put_walls = _strikes(data.get("major_put_walls"))
        cw = _num(data.get("call_wall"))
        pw = _num(data.get("put_wall"))
        if cw is not None:
            call_walls.append(cw)
        if pw is not None:
            put_walls.append(pw)

        highest_call = max(call_walls) if call_walls else cw
        lowest_put = min(put_walls) if put_walls else pw
        mid = gvwap if gvwap is not None else gamma_flip

        if lowest_put is None or highest_call is None or mid is None:
            log.warning("Gammagamma %s: incomplete levels", ticker)
            return None

        return Levels(
            ticker=ticker,
            lower=lowest_put,
            mid=mid,
            top=highest_call,
            gvwap=gvwap,
            gamma_flip=gamma_flip,
            lowest_put=lowest_put,
            highest_call=highest_call,
            spot=spot,
            expiry=str(data.get("expiry_filter", self.expiry)),
        )
