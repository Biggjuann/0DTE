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
                 invalidate_fn: Optional[Callable[[], None]] = None, timeout: float = 10.0):
        self.token_fn = token_fn
        self.invalidate_fn = invalidate_fn or (lambda: None)
        self.base = (base_url or settings.schwab_base_url).rstrip("/")
        self.account_hash = account_hash or settings.schwab_account_hash
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.client = httpx.Client(timeout=timeout)
        self.last_error: Optional[str] = None

    def _headers(self) -> dict:
        tok = self.token_fn()
        return {"Authorization": f"Bearer {tok}", "Accept": "application/json"} if tok else {}

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[httpx.Response]:
        """GET with one automatic retry: on 401 the shared token has rotated,
        so invalidate the cache, re-fetch, and try again."""
        for attempt in (0, 1):
            try:
                r = self.client.get(url, headers=self._headers(), params=params)
            except Exception as exc:  # pragma: no cover - network
                self.last_error = f"network: {exc}"
                return None
            if r.status_code == 401 and attempt == 0:
                log.warning("schwab 401 — refreshing shared token and retrying")
                self.invalidate_fn()
                continue
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}"
            else:
                self.last_error = None
            return r
        return r

    # --- MarketData ----------------------------------------------------------
    def get_quote(self, ticker: str) -> Optional[Quote]:
        """Real-time `last` (ticks continuously) + last completed 1-min close.

        The displayed/trigger split matters: `last` updates every poll so the
        dashboard ticks live, while `minute_close` is the value the entry rule
        ("1-minute close within $1 of lower") evaluates against.
        """
        last = self._realtime_last(ticker)
        mclose = self._minute_close(ticker)
        if last is None and mclose is None:
            log.warning("schwab: no quote for %s (%s)", ticker, self.last_error or "token/entitlement/market?")
            return None
        last = last if last is not None else mclose
        mclose = mclose if mclose is not None else last
        return Quote(ticker=ticker, last=last, minute_close=mclose)

    def _realtime_last(self, ticker: str) -> Optional[float]:
        r = self._get(f"{self.base}/marketdata/v1/{ticker}/quotes")
        if r is not None and r.status_code == 200:
            try:
                j = r.json().get(ticker, {})
                q = j.get("quote", j)
                px = (q.get("lastPrice") or q.get("mark")
                      or q.get("extendedMarketLastPrice") or q.get("closePrice"))
                if px:
                    return float(px)
            except Exception as exc:  # pragma: no cover - network
                log.debug("schwab realtime quote %s parse failed: %s", ticker, exc)
        return None

    def _minute_close(self, ticker: str) -> Optional[float]:
        r = self._get(
            f"{self.base}/marketdata/v1/pricehistory",
            params={
                "symbol": ticker, "periodType": "day", "period": 1,
                "frequencyType": "minute", "frequency": 1,
                "needExtendedHoursData": "true",
            },
        )
        if r is not None and r.status_code == 200:
            try:
                candles = r.json().get("candles") or []
                if candles:
                    return float(candles[-1]["close"])
            except Exception as exc:  # pragma: no cover - network
                log.debug("schwab pricehistory %s parse failed: %s", ticker, exc)
        return None

    def _today(self) -> str:
        return dt.date.today().isoformat()

    def _chains(self, ticker: str, contract_type: str, map_key: str, width: float) -> List[OptionContract]:
        # Pull a window of strikes around the money (do NOT pin to a rounded
        # strike) so the caller can choose the closest *actual* listed strike.
        r = self._get(
            f"{self.base}/marketdata/v1/chains",
            params={
                "symbol": ticker, "contractType": contract_type,
                "fromDate": self._today(), "toDate": self._today(),
                "strikeCount": max(21, int(width * 2 + 1)),
            },
        )
        if r is None or r.status_code != 200:
            log.warning("schwab chains %s %s -> %s", ticker, contract_type,
                        r.status_code if r else "no response")
            return []
        try:
            out: List[OptionContract] = []
            for _exp, strikes in (r.json().get(map_key) or {}).items():
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
            log.warning("schwab chains %s parse failed: %s", ticker, exc)
            return []

    def get_0dte_calls(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]:
        return self._chains(ticker, "CALL", "callExpDateMap", width)

    def get_0dte_puts(self, ticker: str, near_strike: float, width: float = 5.0) -> List[OptionContract]:
        return self._chains(ticker, "PUT", "putExpDateMap", width)

    def get_prior_ohlc(self, ticker: str, timeframe: str = "daily") -> Optional[dict]:
        """High/low/close of the prior completed period (for pivots).

        weekly -> the last weekly candle that fully completed before this week.
        daily  -> the last daily candle before today.
        """
        weekly = str(timeframe).lower().startswith("week")
        params = ({"symbol": ticker, "periodType": "year", "period": 1,
                   "frequencyType": "weekly", "frequency": 1, "needExtendedHoursData": "false"}
                  if weekly else
                  {"symbol": ticker, "periodType": "month", "period": 1,
                   "frequencyType": "daily", "frequency": 1, "needExtendedHoursData": "false"})
        r = self._get(f"{self.base}/marketdata/v1/pricehistory", params=params)
        if r is None or r.status_code != 200:
            return None
        try:
            candles = r.json().get("candles") or []
            if not candles:
                return None
            today = dt.date.today()
            # Weekly: exclude the in-progress week (any candle whose start is within
            # the last 7 days) — robust to Sunday- vs Monday-anchored candles.
            cutoff = today - dt.timedelta(days=7) if weekly else today
            prior = None
            for c in reversed(candles):
                d = dt.datetime.fromtimestamp(c["datetime"] / 1000).date()
                if (d <= cutoff) if weekly else (d < cutoff):
                    prior = c
                    break
            prior = prior or candles[-1]
            return {"high": float(prior["high"]), "low": float(prior["low"]),
                    "close": float(prior["close"])}
        except Exception as exc:  # pragma: no cover - network
            log.warning("schwab pivots ohlc %s (%s) failed: %s", ticker, timeframe, exc)
            return None

    def get_prior_day_ohlc(self, ticker: str) -> Optional[dict]:
        return self.get_prior_ohlc(ticker, "daily")

    def get_contract(self, symbol: str) -> Optional[OptionContract]:
        r = self._get(f"{self.base}/marketdata/v1/{symbol}/quotes")
        if r is not None and r.status_code == 200:
            try:
                j = r.json().get(symbol, {})
                q = j.get("quote", j)
                return OptionContract(
                    symbol=symbol, strike=0.0, expiry="0dte",
                    bid=float(q.get("bidPrice") or 0),
                    ask=float(q.get("askPrice") or 0),
                    last=float(q.get("lastPrice") or q.get("mark") or 0),
                )
            except Exception as exc:  # pragma: no cover - network
                log.debug("schwab option quote %s parse failed: %s", symbol, exc)
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
        url = f"{self.base}/trader/v1/accounts/{self.account_hash}/orders"
        try:
            for attempt in (0, 1):
                r = self.client.post(
                    url, headers={**self._headers(), "Content-Type": "application/json"},
                    json=payload,
                )
                if r.status_code == 401 and attempt == 0:
                    self.invalidate_fn()  # token rotated — refresh and retry
                    continue
                break
            if r.status_code in (200, 201):
                order_id = r.headers.get("location", "").rstrip("/").split("/")[-1]
                return Fill(True, price, f"{instruction} accepted", order_id)
            return Fill(False, price, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as exc:  # pragma: no cover - network
            return Fill(False, price, f"order error: {exc}")

    def buy_to_open(self, contract: OptionContract, qty: int) -> Fill:
        return self._place(contract, qty, "BUY_TO_OPEN")

    def sell_to_close(self, contract: OptionContract, qty: int) -> Fill:
        return self._place(contract, qty, "SELL_TO_CLOSE")

    # Strategy-1 aliases.
    buy_to_open_call = buy_to_open
    sell_to_close_call = sell_to_close
