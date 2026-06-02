"""Live MM provider.

Verified contract (https://web-production-fff5c.up.railway.app):

    GET /analysis?symbol=QQQ
    {
      "symbol":"QQQ","stance":"long","headline":"Long bias confirmed — ride it",
      "details":[
        "BULLS in control: puts below spot 1,490,791 > calls ≥ spot 915,856.",
        "Calls hedging: BEARISH — ≥ spot 915,856 > below spot 900,800.",
        ...
      ],
      "warnings":[]
    }

    GET /auth/token   (header: Authorization: Bearer <SCHWAB_TOKEN_SHARE_KEY>)
    -> the shared Schwab access token used for market data + trading.

`stance` is the trend status. The puts/calls volumes are embedded in the
`details` text, so we regex them out for the bull-control confirmation gate
and the dashboard flow panel.
"""
from __future__ import annotations

import logging
import re
import time
from typing import List, Optional

import httpx

from app.config import settings
from app.models import MMSignal, MMStatus

log = logging.getLogger("mm")

_PUTS_BELOW = re.compile(r"puts below spot\s*([\d,]+)", re.I)
_CALLS_ABOVE = re.compile(r"calls\s*[≥>=]+\s*spot\s*([\d,]+)", re.I)
_CALLS_BELOW = re.compile(r"below spot\s*([\d,]+)\s*$", re.I)  # calls-hedging line tail


def _num_from(patterns, text: str) -> float:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return float(m.group(1).replace(",", ""))
    return 0.0


class MMProviderLive:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, timeout: float = 8.0):
        self.base = (base_url or settings.mm_base_url).rstrip("/")
        self.api_key = api_key or settings.mm_api_key
        self.client = httpx.Client(timeout=timeout)
        self._token_provider = None  # lazy SharedTokenProvider

    # --- trend + flow --------------------------------------------------------
    def get_signal(self, ticker: str) -> Optional[MMSignal]:
        try:
            r = self.client.get(f"{self.base}/analysis", params={"symbol": ticker})
            if r.status_code != 200:
                log.warning("MM /analysis %s -> %s", ticker, r.status_code)
                return None
            d = r.json()
        except Exception as exc:  # pragma: no cover - network
            log.warning("MM /analysis %s failed: %s", ticker, exc)
            return None

        details: List[str] = d.get("details") or []
        joined = " \n ".join(details)
        puts_below = _num_from([_PUTS_BELOW], joined)
        calls_above = _num_from([_CALLS_ABOVE], joined)
        # The "calls hedging" line carries the calls-below-spot figure.
        calls_below = 0.0
        for line in details:
            if "hedging" in line.lower():
                calls_below = _num_from([_CALLS_BELOW], line) or calls_below

        reasoning = list(details)
        if d.get("headline"):
            reasoning = [d["headline"]] + reasoning

        return MMSignal(
            ticker=ticker,
            status=MMStatus.parse(d.get("stance")),
            puts_below_spot=puts_below,
            puts_at_above_spot=0.0,  # not published separately by MM
            calls_below_spot=calls_below,
            calls_at_above_spot=calls_above,
            reasoning=reasoning,
        )

    # --- shared Schwab token -------------------------------------------------
    def get_schwab_token(self) -> Optional[str]:
        """Delegates to the shared-token provider (SCHWAB_TOKEN_URL/SHARE_KEY)."""
        if self._token_provider is None:
            from app.clients.token import SharedTokenProvider
            self._token_provider = SharedTokenProvider()
        return self._token_provider.get_token()
