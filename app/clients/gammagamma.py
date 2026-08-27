"""Live levels provider — Gammagamma backend.

Verified contract (https://gammagamma-production.up.railway.app):

    GET /api/levels/{symbol}?expiry=weekly
    {
      "underlying":"SPY","spot":756.7,"expiry_filter":"weekly",
      "call_wall":760.0,"put_wall":755.0,"gamma_flip":759.0,"gvwap":756.66,
      "major_call_walls":[760.0,755.0,758.0],
      "major_put_walls":[755.0,757.0,756.0]
    }

Level derivation: read the GEX-dominant call_wall / put_wall for EACH
configured expiry (default weekly + 0dte), then take the outer bounds:
    lower = LOWEST put wall across the expiries
    top   = HIGHEST call wall across the expiries
    mid   = GVWAP (weekly preferred) else gamma_flip

The scalar put_wall/call_wall fields are the highest-|GEX| walls per expiry
(arrays are GEX-ranked, largest first).

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
        # One or more expiries (comma-separated), e.g. "weekly,0dte". The
        # channel bounds are taken across ALL of them: highest call wall and
        # lowest put wall.
        raw = expiry or settings.gamma_expiry
        self.expiries = [e.strip() for e in raw.split(",") if e.strip()] or ["weekly"]
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

    def _dominant_walls(self, d: dict):
        """(call_wall, put_wall) for one expiry — the GEX-dominant scalar walls,
        falling back to the GEX-ranked array heads."""
        cw = _num(d.get("call_wall"))
        if cw is None:
            mc = _strikes(d.get("major_call_walls"))
            cw = mc[0] if mc else None
        pw = _num(d.get("put_wall"))
        if pw is None:
            mp = _strikes(d.get("major_put_walls"))
            pw = mp[0] if mp else None
        return cw, pw

    def get_levels(self, ticker: str) -> Optional[Levels]:
        # Pull every configured expiry (e.g. weekly + 0dte).
        datas = [(e, self._get(ticker, e)) for e in self.expiries]
        datas = [(e, d) for e, d in datas if d]
        if not datas:
            allx = self._get(ticker, "all")  # after-hours fallback
            if allx:
                datas = [("all", allx)]
        if not datas:
            log.warning("Gammagamma: no levels for %s", ticker)
            return None

        call_walls, put_walls = [], []
        gvwap = gamma_flip = spot = None
        weekly_gvwap = weekly_flip = None
        used = []
        for e, d in datas:
            used.append(str(d.get("expiry_filter", e)))
            cw, pw = self._dominant_walls(d)
            if cw is not None:
                call_walls.append(cw)
            if pw is not None:
                put_walls.append(pw)
            g, f, s = _num(d.get("gvwap")), _num(d.get("gamma_flip")), _num(d.get("spot"))
            if s is not None:
                spot = s
            if e == "weekly" or d.get("expiry_filter") == "weekly":
                weekly_gvwap, weekly_flip = g, f
            if g is not None and gvwap is None:
                gvwap = g
            if f is not None and gamma_flip is None:
                gamma_flip = f

        # Outer bounds across the expiries: highest call wall, lowest put wall.
        top_call = max(call_walls) if call_walls else None
        bot_put = min(put_walls) if put_walls else None
        # Mid magnet prefers the weekly GVWAP, else any GVWAP, else gamma flip.
        mid = (weekly_gvwap if weekly_gvwap is not None
               else gvwap if gvwap is not None
               else weekly_flip if weekly_flip is not None else gamma_flip)

        if bot_put is None or top_call is None or mid is None:
            log.warning("Gammagamma %s: incomplete levels", ticker)
            return None

        return Levels(
            ticker=ticker,
            lower=bot_put,
            mid=mid,
            top=top_call,
            gvwap=gvwap,
            gamma_flip=gamma_flip,
            lowest_put=bot_put,      # lowest put wall across expiries
            highest_call=top_call,   # highest call wall across expiries
            spot=spot,
            expiry="+".join(used),
        )
