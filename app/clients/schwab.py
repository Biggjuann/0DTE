"""Schwab Trader API adapter — quotes, 0DTE call chain, and order placement.

The OAuth access token is the *shared* token owned by the MM service; we pull
it through the MM provider rather than holding Schwab credentials here.

Long-only is a hard invariant: this broker only ever emits BUY_TO_OPEN /
SELL_TO_CLOSE on calls. When DRY_RUN is set, orders are simulated against the
live quote and never transmitted.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, List, Optional

import httpx

from app.clients.base import Fill
from app.config import settings
from app.models import OptionContract, Quote

log = logging.getLogger("schwab")

TokenFn = Callable[[], Optional[str]]


class SchwabClient:
    """Implements MarketData + Broker."""

    def __init__(self, token_fn: TokenFn, base_url: Optional[str] = None,
                 account_hash: Optional[str] = None, dry_run: Optional[bool] = None,
                 timeout: float = 10.0):
        self.token_fn = token_fn
        self.base = (base_url or settings.schwab_base_url).rstrip("/")
        self.account_hash = account_hash or settings.schwab_account_hash
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict:
        tok = self.token_fn()
        return {"Authorization": f"Bearer {tok}", "Accept": "application/json"} if tok else {}

    # --- MarketData ----------------------------------------------------------
    def get_quote(self, ticker: str) -> Optional[Quote]:
        """Use the last completed 1-minute candle close as minute_close."""
        try:
            r = self.client.get(
                f"{self.base}/marketdata/v1/pricehistory",
                headers=self._headers(),
                params={
                    "symbol": ticker,
                    "periodType": "day",
                    "period": 1,
                    "frequencyType": "minute",
                    "frequency": 1,
                    "needExtendedHoursData": "false",
                },
            )
            if r.status_code == 200:
                candles = r.json().get("candles") or []
                if candles:
                    last = candles[-1]
                    return Quote(ticker=ticker, last=float(last["close"]),
                                 minute_close=float(last["close"]))
        except Exception as exc:  # pragma: no cover - network
            log.debug("schwab pricehistory %s failed: %s", ticker, exc)
        # Fallback: plain quote.
        try:
            r = self.client.get(f"{self.base}/marketdata/v1/{ticker}/quotes",
                                 headers=self._headers())
            if r.status_code == 200:
                j = r.json().get(ticker, {})
                q = j.get("quote", j)
                px = float(q.get("lastPrice") or q.get("mark") or 0)
                if px:
                    return Quote(ticker=ticker, last=px, minute_close=px)
        except Exception as exc:  # pragma: no cover - network
            log.debug("schwab quote %s failed: %s", ticker, exc)
        return None

    def _today(self) -> str:
        return dt.date.today().isoformat()

    def get_0dte_calls(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]:
        try:
            r = self.client.get(
                f"{self.base}/marketdata/v1/chains",
                headers=self._headers(),
                params={
                    "symbol": ticker,
                    "contractType": "CALL",
                    "fromDate": self._today(),
                    "toDate": self._today(),
                    "strikeCount": int(width * 2 + 1),
                    "strike": round(near_strike),
                },
            )
            if r.status_code != 200:
                log.warning("schwab chains %s -> %s", ticker, r.status_code)
                return []
            out: List[OptionContract] = []
            for _exp, strikes in (r.json().get("callExpDateMap") or {}).items():
                for _k, legs in strikes.items():
                    for leg in legs:
                        out.append(OptionContract(
                            symbol=leg["symbol"],
                            strike=float(leg["strikePrice"]),
                            expiry=_exp.split(":")[0],
                            bid=float(leg.get("bid") or 0),
                            ask=float(leg.get("ask") or 0),
                            last=float(leg.get("last") or leg.get("mark") or 0),
                        ))
            return out
        except Exception as exc:  # pragma: no cover - network
            log.warning("schwab chains %s failed: %s", ticker, exc)
            return []

    def get_contract(self, symbol: str) -> Optional[OptionContract]:
        try:
            r = self.client.get(f"{self.base}/marketdata/v1/{symbol}/quotes",
                                 headers=self._headers())
            if r.status_code == 200:
                j = r.json().get(symbol, {})
                q = j.get("quote", j)
                return OptionContract(
                    symbol=symbol, strike=0.0, expiry="0dte",
                    bid=float(q.get("bidPrice") or 0),
                    ask=float(q.get("askPrice") or 0),
                    last=float(q.get("lastPrice") or q.get("mark") or 0),
                )
        except Exception as exc:  # pragma: no cover - network
            log.debug("schwab option quote %s failed: %s", symbol, exc)
        return None

    # --- Broker (long-only) --------------------------------------------------
    def _place(self, contract: OptionContract, qty: int, instruction: str) -> Fill:
        assert instruction in ("BUY_TO_OPEN", "SELL_TO_CLOSE"), "long-only invariant"
        price = contract.ask if instruction == "BUY_TO_OPEN" else contract.bid
        price = price or contract.mid
        if self.dry_run:
            return Fill(True, price, f"DRY_RUN {instruction}", "dry-run")
        if not self.account_hash:
            return Fill(False, price, "SCHWAB_ACCOUNT_HASH not set")
        payload = {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "price": f"{price:.2f}",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": instruction,
                "quantity": qty,
                "instrument": {"symbol": contract.symbol, "assetType": "OPTION"},
            }],
        }
        try:
            r = self.client.post(
                f"{self.base}/trader/v1/accounts/{self.account_hash}/orders",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
            )
            if r.status_code in (200, 201):
                order_id = r.headers.get("location", "").rstrip("/").split("/")[-1]
                return Fill(True, price, f"{instruction} accepted", order_id)
            return Fill(False, price, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as exc:  # pragma: no cover - network
            return Fill(False, price, f"order error: {exc}")

    def buy_to_open_call(self, contract: OptionContract, qty: int) -> Fill:
        return self._place(contract, qty, "BUY_TO_OPEN")

    def sell_to_close_call(self, contract: OptionContract, qty: int) -> Fill:
        return self._place(contract, qty, "SELL_TO_CLOSE")
