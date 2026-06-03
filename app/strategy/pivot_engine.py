"""Person's-pivots strategy engine (second tab).

Regime is set by VIX vs its own daily pivot:
  * VIX  > VIX pivot  -> bearish: look to SHORT (buy puts) when QQQ/SPY hits R1.
  * VIX <= VIX pivot  -> bullish: look to LONG  (buy calls) when QQQ/SPY hits S1.

The contract strike is the closest listed to the pivot (PP). On entry we buy
PIVOT_CONTRACTS; we scale 50% out at the pivot, move the stop to breakeven, and
run the remainder to the opposite level (S1 for shorts, R1 for longs). The
initial stop is PIVOT_STOP_PCT of the entry premium (default 50%).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.clients.factory import PivotProviders, build_pivot_providers
from app.config import settings
from app.models import OptionContract, PivotLevels, PivotPosition, Quote, TradeRecord, to_jsonable
from app.store import TradeLog

log = logging.getLogger("pivot")

DEFAULT_PIVOT_LOG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pivot_trades.json")


@dataclass
class PivotTickerState:
    ticker: str
    pivots: Optional[PivotLevels] = None
    quote: Optional[Quote] = None
    state: str = "disabled"          # disabled / armed / open
    position: Optional[PivotPosition] = None
    can_enter: bool = True           # one entry per level touch
    last_event: str = "waiting for data"
    events: List[str] = field(default_factory=list)
    updated: float = field(default_factory=time.time)

    def log_event(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.last_event = msg
        self.events.append(f"{ts}  {msg}")
        self.events = self.events[-25:]
        log.info("[%s] %s", self.ticker, msg)


class PivotEngine:
    def __init__(self, providers: Optional[PivotProviders] = None):
        self.providers = providers or build_pivot_providers()
        self.trades = TradeLog(path=settings.pivot_trade_log_path or DEFAULT_PIVOT_LOG)
        self.states: Dict[str, PivotTickerState] = {t: PivotTickerState(t) for t in settings.tickers}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.auto_trade = True
        # VIX regime.
        self.vix_last: Optional[float] = None
        self.vix_pp: Optional[float] = None
        self.regime: Optional[str] = None   # "bearish" / "bullish"
        self._last_pivots: Dict[str, float] = {}
        self._last_quote: Dict[str, float] = {}
        self._last_vix: float = 0.0

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="pivot-engine", daemon=True)
        self._thread.start()
        log.info("pivot engine started (mode=%s, dry_run=%s)", self.providers.mode, settings.dry_run)

    def stop(self) -> None:
        self.running = False
        self._stop.set()

    def kill_switch(self) -> None:
        with self._lock:
            for st in self.states.values():
                if st.position:
                    self._close(st, st.position.remaining_qty, "KILL", "KILL SWITCH — flatten all")
            self.auto_trade = False
        log.warning("PIVOT KILL SWITCH engaged")

    # --- loop ----------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover
                log.exception("pivot tick error")
            self._stop.wait(1.0)

    def _refresh_vix(self) -> None:
        sym = self.providers.vix_symbol
        q = self.providers.market.get_quote(sym)
        pv = self.providers.pivots.get_pivots(sym)
        if q:
            self.vix_last = q.last
        if pv:
            self.vix_pp = pv.pp
        if self.vix_last is not None and self.vix_pp is not None:
            self.regime = "bearish" if self.vix_last > self.vix_pp else "bullish"

    def _tick(self) -> None:
        now = time.time()
        with self._lock:
            if now - self._last_vix >= settings.mm_poll_seconds:
                self._refresh_vix()
                self._last_vix = now
            for ticker, st in self.states.items():
                if now - self._last_pivots.get(ticker, 0) >= max(60, settings.levels_poll_seconds):
                    pv = self.providers.pivots.get_pivots(ticker)
                    if pv:
                        st.pivots = pv
                    self._last_pivots[ticker] = now
                if now - self._last_quote.get(ticker, 0) >= settings.quote_poll_seconds:
                    q = self.providers.market.get_quote(ticker)
                    if q:
                        st.quote = q
                    self._last_quote[ticker] = now
                self._evaluate(st)
                st.updated = now

    # --- state machine -------------------------------------------------------
    def _evaluate(self, st: PivotTickerState) -> None:
        if not (st.pivots and st.quote and self.regime):
            return
        pv, price = st.pivots, st.quote.last
        prox = settings.pivot_proximity
        if st.pivots.spot != price:
            st.pivots.spot = price

        if st.position is None:
            if self.regime == "bearish":
                # SHORT setup: enter when price reaches R1.
                if price < pv.r1 - prox:
                    st.can_enter = True
                in_zone = price >= pv.r1 - prox
                if in_zone and st.can_enter:
                    self._enter(st, "SHORT")
                else:
                    self._arm(st, f"bearish (VIX {self.vix_last} > {self.vix_pp}); "
                                  f"price {price:.2f} not at R1 {pv.r1:.2f}")
            else:
                # LONG setup: enter when price reaches S1.
                if price > pv.s1 + prox:
                    st.can_enter = True
                in_zone = price <= pv.s1 + prox
                if in_zone and st.can_enter:
                    self._enter(st, "LONG")
                else:
                    self._arm(st, f"bullish (VIX {self.vix_last} <= {self.vix_pp}); "
                                  f"price {price:.2f} not at S1 {pv.s1:.2f}")
        else:
            self._manage(st)

    def _arm(self, st: PivotTickerState, why: str) -> None:
        if st.state != "armed":
            st.log_event(f"ARMED — {why}")
        st.state = "armed"

    # --- entries -------------------------------------------------------------
    def _pick(self, ticker: str, option_type: str, target: float) -> Optional[OptionContract]:
        chain = (self.providers.market.get_0dte_puts(ticker, near_strike=target)
                 if option_type == "PUT"
                 else self.providers.market.get_0dte_calls(ticker, near_strike=target))
        if not chain:
            return None
        return min(chain, key=lambda c: abs(c.strike - target))

    def _enter(self, st: PivotTickerState, direction: str) -> None:
        pv = st.pivots
        option_type = "PUT" if direction == "SHORT" else "CALL"
        target = pv.s1 if direction == "SHORT" else pv.r1
        if not self.auto_trade:
            st.log_event(f"{direction} signal at {'R1' if direction=='SHORT' else 'S1'} "
                         f"(auto-trade OFF — not sent)")
            st.state = "armed"
            return
        contract = self._pick(st.ticker, option_type, pv.pp)
        if not contract:
            st.log_event("ENTRY blocked — no 0DTE chain")
            st.state = "armed"
            return
        fill = self.providers.broker.buy_to_open(contract, settings.pivot_contracts)
        if not fill.accepted:
            st.log_event(f"ENTRY rejected: {fill.message}")
            st.state = "armed"
            return
        qty = settings.pivot_contracts
        st.position = PivotPosition(
            ticker=st.ticker, direction=direction, option_type=option_type,
            contract_symbol=contract.symbol, strike=contract.strike, expiry=contract.expiry,
            qty=qty, remaining_qty=qty, entry_price=fill.price, entry_time=time.time(),
            entry_underlying=st.quote.last, pp=pv.pp, target=target,
            current_price=fill.price, last_underlying=st.quote.last,
            stop_premium=round(fill.price * (1 - settings.pivot_stop_pct), 2),
        )
        st.state = "open"
        st.can_enter = False
        trig = "R1" if direction == "SHORT" else "S1"
        st.log_event(f"ENTRY {direction} {qty}x {contract.symbol} @ {fill.price:.2f} "
                     f"(strike {contract.strike:g} = PP; trigger {trig}; stop {st.position.stop_premium:.2f})")
        self.trades.append(self._record("ENTRY", st.position, qty, fill.price, None, None))

    # --- management ----------------------------------------------------------
    def _manage(self, st: PivotTickerState) -> None:
        pos = st.position
        c = self.providers.market.get_contract(pos.contract_symbol)
        if c and c.mid:
            pos.current_price = c.mid
        pos.last_underlying = st.quote.last
        price = st.quote.last
        pv = st.pivots

        # 1) Premium stop (50% of value, or breakeven after the scale).
        if pos.current_price <= pos.stop_premium:
            label = "breakeven stop" if pos.breakeven else "50% premium stop"
            self._close(st, pos.remaining_qty, "STOP", f"{label} @ {pos.current_price:.2f}")
            return

        # 2) Scale 50% at the pivot, then move stop to breakeven.
        reached_pivot = (price <= pv.pp) if pos.direction == "SHORT" else (price >= pv.pp)
        if reached_pivot and not pos.scaled:
            half = max(1, int(round(pos.qty * settings.pivot_scale_pct)))
            half = min(half, pos.remaining_qty)
            self._close(st, half, "SCALE", f"50% off at pivot {pv.pp:.2f}", keep_open=True)
            pos.scaled = True
            pos.breakeven = True
            pos.stop_premium = round(pos.entry_price, 2)  # breakeven
            st.log_event(f"stop moved to breakeven {pos.stop_premium:.2f}")
            if pos.remaining_qty <= 0:
                st.position = None
                st.state = "armed"
                return

        # 3) Final target: S1 (short) / R1 (long).
        hit_target = (price <= pos.target) if pos.direction == "SHORT" else (price >= pos.target)
        if hit_target:
            tlabel = "S1" if pos.direction == "SHORT" else "R1"
            self._close(st, pos.remaining_qty, "TARGET", f"target {tlabel} {pos.target:.2f}")

    def _close(self, st: PivotTickerState, qty: int, exit_type: str, reason: str,
               keep_open: bool = False) -> None:
        pos = st.position
        if not pos or qty <= 0:
            return
        contract = self.providers.market.get_contract(pos.contract_symbol) or OptionContract(
            symbol=pos.contract_symbol, strike=pos.strike, expiry=pos.expiry,
            bid=pos.current_price, ask=pos.current_price, last=pos.current_price)
        fill = self.providers.broker.sell_to_close(contract, qty)
        if not fill.accepted:
            st.log_event(f"EXIT rejected: {fill.message}")
            return
        leg_pnl = round((fill.price - pos.entry_price) * 100 * qty, 2)
        pos.realized_pnl = round(pos.realized_pnl + leg_pnl, 2)
        pos.remaining_qty -= qty
        st.log_event(f"{exit_type} {qty}x {pos.contract_symbol} @ {fill.price:.2f} "
                     f"P&L ${leg_pnl:+.2f} — {reason}")
        self.trades.append(self._record("EXIT", pos, qty, fill.price, leg_pnl, exit_type, reason))
        if not keep_open and pos.remaining_qty <= 0:
            st.position = None
            st.state = "armed"

    def _record(self, action: str, pos: PivotPosition, qty: int, price: float,
                pnl, exit_type, reason: str = "") -> TradeRecord:
        return TradeRecord(
            ts=time.time(), ticker=pos.ticker, action=action, exit_type=exit_type,
            reason=reason or f"{pos.direction} pivot {pos.option_type}",
            underlying=pos.last_underlying, contract_symbol=pos.contract_symbol,
            strike=pos.strike, qty=qty, price=price, pnl=pnl, dry_run=settings.dry_run,
            stance=self.regime, entry_price=pos.entry_price, entry_underlying=pos.entry_underlying,
            lower=pos.target, mid=pos.pp, top=None,
        )

    # --- snapshot ------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            tickers = []
            for st in self.states.values():
                pos = st.position
                tickers.append({
                    "ticker": st.ticker,
                    "state": st.state,
                    "pivots": to_jsonable(st.pivots) if st.pivots else None,
                    "quote": to_jsonable(st.quote) if st.quote else None,
                    "direction": (self.regime == "bearish" and "SHORT") or
                                 (self.regime == "bullish" and "LONG") or None,
                    "position": to_jsonable(pos) if pos else None,
                    "open_pnl": pos.open_pnl if pos else None,
                    "total_pnl": pos.total_pnl if pos else None,
                    "last_event": st.last_event,
                    "events": list(reversed(st.events)),
                    "updated": st.updated,
                })
            return {
                "mode": self.providers.mode,
                "dry_run": settings.dry_run,
                "running": self.running,
                "auto_trade": self.auto_trade,
                "contracts": settings.pivot_contracts,
                "vix_symbol": self.providers.vix_symbol,
                "vix_last": self.vix_last,
                "vix_pp": self.vix_pp,
                "regime": self.regime,
                "tickers": tickers,
                "trades": self.trades.recent(40),
                "summary": self.trades.summary(),
                "server_time": time.time(),
            }
