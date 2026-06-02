"""The long-only 0DTE strategy engine.

Per-ticker state machine
=========================

    DISABLED  MM status is not long / cautious-long -> no approval.
    ARMED     Approved (long or cautious-long) + bull-control flow, waiting
              for price to close within $1 of the LOWER level (lowest put wall)
              on the 1-minute chart.
    OPEN      Holding a long 0DTE call struck at the MID level.

Entry  (FLAT/ARMED -> OPEN)
    status in {LONG, CAUTIOUS_LONG}                     (approval)
    AND bull_control (puts below spot > calls >= spot)  (flow confirmation)
    AND |minute_close - lower| <= PROXIMITY             (1-min trigger)
        -> BUY_TO_OPEN 0DTE call, strike = nearest to (MID + STRIKE_OFFSET),
           i.e. $1 above the mid level by default.

Exit  (OPEN -> flat), checked in priority order
    a) status loses long approval entirely (neutral/short) -> protective exit.
    b) 1-minute CLOSE below the LOWER level                -> STOP.
    c) status is CAUTIOUS_LONG and price >= mid - $1        -> take profit at MID.
    d) status stays LONG and price >= top - $1 (top wall    -> take profit at TOP.
       label "flashing")
Take-profits use a reached-or-beyond band so a fast 0DTE move that overshoots
the level between quote polls still closes the trade.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.clients.factory import Providers, build_providers
from app.config import settings
from app.models import (Levels, MMSignal, MMStatus, OptionContract, Position,
                        PositionState, Quote, TradeRecord, to_jsonable)
from app.store import TradeLog

log = logging.getLogger("engine")


@dataclass
class TickerState:
    ticker: str
    levels: Optional[Levels] = None
    mm: Optional[MMSignal] = None
    quote: Optional[Quote] = None
    state: PositionState = PositionState.DISABLED
    position: Optional[Position] = None
    prev_status: Optional[MMStatus] = None
    saw_long_in_trade: bool = False
    last_event: str = "waiting for data"
    events: List[str] = field(default_factory=list)
    updated: float = field(default_factory=time.time)

    def log_event(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.last_event = msg
        self.events.append(f"{ts}  {msg}")
        self.events = self.events[-25:]
        log.info("[%s] %s", self.ticker, msg)


class Engine:
    def __init__(self, providers: Optional[Providers] = None):
        self.providers = providers or build_providers()
        self.trades = TradeLog()
        self.states: Dict[str, TickerState] = {t: TickerState(t) for t in settings.tickers}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Master switches.
        self.running = False
        self.auto_trade = True   # if False: signals only, no orders (paper-watch)
        # Refresh bookkeeping.
        self._last_levels: Dict[str, float] = {}
        self._last_mm: Dict[str, float] = {}
        self._last_quote: Dict[str, float] = {}

    # --- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="engine", daemon=True)
        self._thread.start()
        log.info("engine started (mode=%s, dry_run=%s)", self.providers.mode, settings.dry_run)

    def stop(self) -> None:
        self.running = False
        self._stop.set()

    def kill_switch(self) -> None:
        """Flatten everything and halt trading."""
        with self._lock:
            for st in self.states.values():
                if st.position:
                    self._exit(st, "KILL SWITCH — flatten all")
            self.auto_trade = False
        log.warning("KILL SWITCH engaged")

    # --- main loop -----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - keep the loop alive
                log.exception("engine tick error")
            self._stop.wait(1.0)

    def _tick(self) -> None:
        now = time.time()
        with self._lock:
            for ticker, st in self.states.items():
                if now - self._last_levels.get(ticker, 0) >= settings.levels_poll_seconds:
                    lv = self.providers.levels.get_levels(ticker)
                    if lv:
                        st.levels = lv
                    self._last_levels[ticker] = now
                if now - self._last_mm.get(ticker, 0) >= settings.mm_poll_seconds:
                    mm = self.providers.mm.get_signal(ticker)
                    if mm:
                        st.mm = mm
                    self._last_mm[ticker] = now
                if now - self._last_quote.get(ticker, 0) >= settings.quote_poll_seconds:
                    q = self.providers.market.get_quote(ticker)
                    if q:
                        st.quote = q
                    self._last_quote[ticker] = now
                self._evaluate(st)
                st.updated = now

    # --- helpers -------------------------------------------------------------
    def _near(self, a: float, b: float) -> bool:
        return abs(a - b) <= settings.proximity

    def _mark_position(self, st: TickerState) -> None:
        if not st.position:
            return
        c = self.providers.market.get_contract(st.position.contract_symbol)
        if c and c.mid:
            st.position.current_price = c.mid
        if st.quote:
            st.position.last_underlying = st.quote.last

    # --- the state machine ---------------------------------------------------
    def _evaluate(self, st: TickerState) -> None:
        if not (st.levels and st.mm and st.quote):
            return

        mm, lv, q = st.mm, st.levels, st.quote
        approved = mm.status.approves_entry
        price_close = q.minute_close
        price = q.last

        if st.position is None:
            if not approved:
                if st.state != PositionState.DISABLED:
                    st.log_event(f"MM status '{mm.status.value}' — trading disabled")
                st.state = PositionState.DISABLED
            else:
                confirm = mm.bull_control
                trigger = self._near(price_close, lv.lower)
                if trigger and confirm:
                    self._enter(st)
                else:
                    if st.state != PositionState.ARMED:
                        why = []
                        if not trigger:
                            why.append(f"close {price_close:.2f} not within "
                                       f"${settings.proximity:g} of lower {lv.lower:.2f}")
                        if not confirm:
                            why.append("flow not bull-control")
                        st.log_event(f"ARMED ({mm.status.value}); waiting — {', '.join(why)}")
                    st.state = PositionState.ARMED
        else:
            self._mark_position(st)
            if mm.status is MMStatus.LONG:
                st.saw_long_in_trade = True

            prox = settings.proximity
            reason = None
            if not approved:
                reason = f"MM status '{mm.status.value}' lost long approval — protective exit"
            elif price_close < lv.lower:
                # Stop: 1-minute close below the lower (put-wall) level.
                reason = (f"STOP — 1-min close {price_close:.2f} below LOWER {lv.lower:.2f}")
            elif mm.status is MMStatus.CAUTIOUS_LONG and price >= lv.mid - prox:
                # Take profit at mid on a long->cautious downgrade (reached the
                # mid zone or beyond; robust to overshoot between polls).
                reason = (f"Downgraded to cautious-long at/above MID {lv.mid:.2f} "
                          f"(within ${prox:g}) — take profit at mid")
            elif mm.status is MMStatus.LONG and price >= lv.top - prox:
                # Held LONG into the top (call-wall) zone -> take profit at top.
                reason = (f"Held LONG into TOP {lv.top:.2f} (within ${prox:g}) "
                          f"— take profit at top call wall")

            if reason:
                self._exit(st, reason)

        st.prev_status = mm.status

    # --- order actions -------------------------------------------------------
    def _pick_call(self, ticker: str, target_strike: float) -> Optional[OptionContract]:
        chain = self.providers.market.get_0dte_calls(ticker, near_strike=target_strike)
        if not chain:
            return None
        return min(chain, key=lambda c: abs(c.strike - target_strike))

    def _enter(self, st: TickerState) -> None:
        lv, mm = st.levels, st.mm
        if not self.auto_trade:
            st.log_event(f"ENTRY signal at lower {lv.lower:.2f} (auto-trade OFF — not sent)")
            st.state = PositionState.ARMED
            return
        target_strike = lv.mid + settings.strike_offset
        contract = self._pick_call(st.ticker, target_strike)
        if not contract:
            st.log_event("ENTRY blocked — no 0DTE call chain available")
            st.state = PositionState.ARMED
            return
        fill = self.providers.broker.buy_to_open_call(contract, settings.contracts)
        if not fill.accepted:
            st.log_event(f"ENTRY rejected by broker: {fill.message}")
            st.state = PositionState.ARMED
            return
        st.position = Position(
            ticker=st.ticker,
            contract_symbol=contract.symbol,
            strike=contract.strike,
            expiry=contract.expiry,
            qty=settings.contracts,
            entry_price=fill.price,
            entry_time=time.time(),
            entry_underlying=st.quote.last,
            current_price=fill.price,
            last_underlying=st.quote.last,
        )
        st.state = PositionState.OPEN
        st.saw_long_in_trade = mm.status is MMStatus.LONG
        st.log_event(f"ENTRY {settings.contracts}x {contract.symbol} @ {fill.price:.2f} "
                     f"(strike {contract.strike:g} = mid {lv.mid:.2f}+${settings.strike_offset:g}; "
                     f"trigger near lower {lv.lower:.2f})")
        self.trades.append(TradeRecord(
            ts=time.time(), ticker=st.ticker, action="ENTRY", reason="lower-level trigger + bull control",
            underlying=st.quote.last, contract_symbol=contract.symbol, strike=contract.strike,
            qty=settings.contracts, price=fill.price, dry_run=settings.dry_run,
        ))

    def _exit(self, st: TickerState, reason: str) -> None:
        pos = st.position
        if not pos:
            return
        contract = self.providers.market.get_contract(pos.contract_symbol) or OptionContract(
            symbol=pos.contract_symbol, strike=pos.strike, expiry=pos.expiry,
            bid=pos.current_price, ask=pos.current_price, last=pos.current_price,
        )
        fill = self.providers.broker.sell_to_close_call(contract, pos.qty)
        if not fill.accepted:
            st.log_event(f"EXIT rejected by broker: {fill.message}")
            return
        pnl = round((fill.price - pos.entry_price) * 100 * pos.qty, 2)
        st.log_event(f"EXIT {pos.contract_symbol} @ {fill.price:.2f}  P&L ${pnl:+.2f} — {reason}")
        self.trades.append(TradeRecord(
            ts=time.time(), ticker=st.ticker, action="EXIT", reason=reason,
            underlying=st.quote.last if st.quote else pos.last_underlying,
            contract_symbol=pos.contract_symbol, strike=pos.strike, qty=pos.qty,
            price=fill.price, pnl=pnl, dry_run=settings.dry_run,
        ))
        st.position = None
        st.saw_long_in_trade = False
        st.state = PositionState.ARMED if (st.mm and st.mm.status.approves_entry) else PositionState.DISABLED

    # --- snapshot for the dashboard -----------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            tickers = []
            for st in self.states.values():
                tickers.append({
                    "ticker": st.ticker,
                    "state": st.state.value,
                    "levels": to_jsonable(st.levels) if st.levels else None,
                    "mm": to_jsonable(st.mm) if st.mm else None,
                    "bull_control": st.mm.bull_control if st.mm else None,
                    "quote": to_jsonable(st.quote) if st.quote else None,
                    "position": to_jsonable(st.position) if st.position else None,
                    "pnl": st.position.pnl if st.position else None,
                    "pnl_pct": st.position.pnl_pct if st.position else None,
                    "last_event": st.last_event,
                    "events": list(reversed(st.events)),
                    "updated": st.updated,
                })
            return {
                "mode": self.providers.mode,
                "dry_run": settings.dry_run,
                "running": self.running,
                "auto_trade": self.auto_trade,
                "contracts": settings.contracts,
                "proximity": settings.proximity,
                "strike_offset": settings.strike_offset,
                "tickers": tickers,
                "trades": self.trades.recent(40),
                "summary": self.trades.summary(),
                "server_time": time.time(),
            }
