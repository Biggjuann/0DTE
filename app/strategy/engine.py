"""The long-only 0DTE strategy engine.

Per-ticker state machine
=========================

    DISABLED  MM status is not long / cautious-long (or levels inverted).
    ARMED     Approved (long or cautious-long), waiting for price to reach the
              put wall (lower level).
    OPEN      Holding a long 0DTE call struck at (MID + STRIKE_OFFSET).

Entry  (FLAT/ARMED -> OPEN) — the ONLY conditions for a long:
    status in {LONG, CAUTIOUS_LONG}                 (approval)
    AND lower <= live price <= lower + PROXIMITY     (at/just above the put wall)
    plus: valid channel (lower < mid < top) and a one-entry-per-touch latch.
        -> BUY_TO_OPEN 0DTE call, strike = closest listed to (MID + STRIKE_OFFSET).

Exit  (OPEN -> flat), checked in priority order
    a) status loses long approval entirely (neutral/short) -> protective exit.
    b) price below the put wall (lower)                    -> STOP.
    c) status DOWNGRADES long -> cautious-long during the trade and
       price >= mid - PROXIMITY                            -> take profit at MID.
    d) price >= top - PROXIMITY while still approved        -> take profit at TOP.
Take-profits use a reached-or-beyond band so a fast 0DTE move that overshoots
the level between quote polls still closes the trade. The MID exit needs a real
downgrade, so entering on cautious-long does not immediately exit.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app import market_hours
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
    can_enter: bool = True       # re-arm latch: one entry per put-wall touch
    cooldown_until: float = 0.0  # anti-whipsaw: no re-entry until this time
    quote_error: Optional[str] = None
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
                    self._exit(st, "KILL SWITCH — flatten all", "KILL")
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
                        st.quote_error = None
                    else:
                        st.quote_error = (getattr(self.providers.market, "last_error", None)
                                          or "quote feed returned nothing")
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
        st.position.update_excursions()

    # --- the state machine ---------------------------------------------------
    def _evaluate(self, st: TickerState) -> None:
        if not (st.levels and st.mm and st.quote):
            return

        mm, lv, q = st.mm, st.levels, st.quote
        approved = mm.status.approves_entry
        price = q.last
        prox = settings.proximity

        # RTH gate: 0DTE trades only during the regular session. Outside it,
        # flatten any open position and never enter.
        ph = market_hours.phase()
        if market_hours.should_flatten(ph):
            if st.position:
                why = "session close" if ph == market_hours.FLATTEN else "outside RTH"
                self._exit(st, f"{why} — flatten 0DTE ({market_hours.label()})", "EOD")
            else:
                msg = f"market closed ({market_hours.label()}) — idle"
                if st.last_event != msg:
                    st.log_event(msg)
                st.state = PositionState.DISABLED
            st.prev_status = mm.status
            return
        entries_open = market_hours.entries_allowed(ph)  # False during 'late'
        # The channel must be properly ordered (lower < mid < top). MIN_CHANNEL_GAP
        # (default 0) can additionally require separation between the legs.
        gap = settings.min_channel_gap
        valid_channel = (lv.lower < lv.mid < lv.top
                         and (lv.mid - lv.lower) >= gap
                         and (lv.top - lv.mid) >= gap)

        if st.position is None:
            # Re-arm latch: once price leaves the put-wall band (upward) we are
            # allowed one entry on the next touch. This prevents re-entering the
            # same touch over and over. REARM_DISTANCE (default = proximity) can
            # widen the band so a noisy hover at the wall doesn't keep re-arming.
            rearm = settings.rearm_distance or prox
            if price > lv.lower + rearm:
                st.can_enter = True
            in_cooldown = time.time() < st.cooldown_until

            if not approved:
                msg = f"MM status '{mm.status.value}' — no long approval"
                if st.last_event != msg:
                    st.log_event(msg)
                st.state = PositionState.DISABLED
            elif not valid_channel:
                msg = (f"levels inverted (lower {lv.lower:.2f} / "
                       f"mid {lv.mid:.2f} / top {lv.top:.2f}) — standing aside")
                if st.last_event != msg:
                    st.log_event(msg)
                st.state = PositionState.DISABLED
            else:
                # THE entry rule: live price at/within $PROX above the put wall
                # (lower) AND stance is long or cautious-long. We require price at
                # or above the wall so we don't enter into an immediate stop.
                at_putwall = lv.lower <= price <= lv.lower + prox
                if at_putwall and st.can_enter and entries_open and not in_cooldown:
                    self._enter(st)
                    st.can_enter = False
                elif at_putwall and st.can_enter and in_cooldown:
                    left = int(st.cooldown_until - time.time())
                    msg = f"signal at put wall but in stop-cooldown ({left}s left) — standing aside"
                    if st.last_event != msg:
                        st.log_event(msg)
                    st.state = PositionState.ARMED
                elif at_putwall and st.can_enter and not entries_open:
                    msg = f"signal at put wall but late session ({market_hours.label()}) — no new entries"
                    if st.last_event != msg:
                        st.log_event(msg)
                    st.state = PositionState.ARMED
                else:
                    if st.state != PositionState.ARMED:
                        if price < lv.lower:
                            why = f"price {price:.2f} below put wall {lv.lower:.2f}"
                        elif not at_putwall:
                            why = (f"price {price:.2f} not within ${prox:g} above "
                                   f"put wall {lv.lower:.2f}")
                        else:
                            why = "awaiting price to leave & re-touch put wall"
                        st.log_event(f"ARMED ({mm.status.value}); waiting — {why}")
                    st.state = PositionState.ARMED
        else:
            self._mark_position(st)
            if mm.status is MMStatus.LONG:
                st.saw_long_in_trade = True

            reason = exit_type = None
            if not approved:
                exit_type = "PROTECTIVE"
                reason = f"MM status '{mm.status.value}' lost long approval — protective exit"
            elif price < lv.lower:
                # Stop: price broke below the put-wall (lower) level.
                exit_type = "STOP"
                reason = (f"STOP — price {price:.2f} below put wall {lv.lower:.2f}")
            elif (mm.status is MMStatus.CAUTIOUS_LONG and st.saw_long_in_trade
                  and price >= lv.mid - prox):
                # Mid take-profit ONLY on a genuine long -> cautious-long
                # DOWNGRADE during the trade (not when we entered on cautious).
                exit_type = "MID"
                reason = (f"Downgraded from long to cautious-long at/above MID "
                          f"{lv.mid:.2f} (within ${prox:g}) — take profit at mid")
            elif price >= lv.top - prox:
                # Reached the top (call-wall) zone while still approved (long or
                # cautious-long) -> take profit at the ceiling.
                exit_type = "TOP"
                reason = (f"Reached TOP {lv.top:.2f} (within ${prox:g}) "
                          f"— take profit at top call wall")

            if reason:
                self._exit(st, reason, exit_type)

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
            entry_lower=lv.lower, entry_mid=lv.mid, entry_top=lv.top,
            entry_stance=mm.status.value, entry_bull_control=mm.bull_control,
            max_premium=fill.price, min_premium=fill.price,
            max_underlying=st.quote.last, min_underlying=st.quote.last,
        )
        st.state = PositionState.OPEN
        st.saw_long_in_trade = mm.status is MMStatus.LONG
        st.log_event(f"ENTRY {settings.contracts}x {contract.symbol} @ {fill.price:.2f} "
                     f"(strike {contract.strike:g} = mid {lv.mid:.2f}+${settings.strike_offset:g}; "
                     f"trigger near lower {lv.lower:.2f})")
        self.trades.append(TradeRecord(
            ts=time.time(), ticker=st.ticker, action="ENTRY",
            reason=f"price within ${settings.proximity:g} of put wall + {mm.status.value}",
            underlying=st.quote.last, contract_symbol=contract.symbol, strike=contract.strike,
            qty=settings.contracts, price=fill.price, dry_run=settings.dry_run,
            stance=mm.status.value, bull_control=mm.bull_control,
            lower=lv.lower, mid=lv.mid, top=lv.top,
        ))

    def _exit(self, st: TickerState, reason: str, exit_type: Optional[str] = None) -> None:
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
        underlying = st.quote.last if st.quote else pos.last_underlying
        st.log_event(f"EXIT {pos.contract_symbol} @ {fill.price:.2f}  P&L ${pnl:+.2f} — {reason}")
        self.trades.append(TradeRecord(
            ts=time.time(), ticker=st.ticker, action="EXIT", reason=reason, exit_type=exit_type,
            underlying=underlying, contract_symbol=pos.contract_symbol, strike=pos.strike, qty=pos.qty,
            price=fill.price, pnl=pnl, dry_run=settings.dry_run,
            stance=st.mm.status.value if st.mm else None,
            bull_control=st.mm.bull_control if st.mm else None,
            lower=pos.entry_lower, mid=pos.entry_mid, top=pos.entry_top,
            entry_price=pos.entry_price, entry_underlying=pos.entry_underlying,
            entry_stance=pos.entry_stance,
            hold_seconds=round(time.time() - pos.entry_time, 1),
            mae=pos.mae, mfe=pos.mfe,
        ))
        st.position = None
        st.saw_long_in_trade = False
        # Anti-whipsaw: after a STOP, sit out for STOP_COOLDOWN_SECONDS so a price
        # hovering at the put wall can't churn enter/stop repeatedly.
        if exit_type == "STOP" and settings.stop_cooldown_seconds > 0:
            st.cooldown_until = time.time() + settings.stop_cooldown_seconds
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
                    "quote_error": st.quote_error,
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
                "session": market_hours.info(),
                "proximity": settings.proximity,
                "strike_offset": settings.strike_offset,
                "market_data_provider": (settings.market_data_provider if self.providers.mode == "live" else "mock"),
                "options_provider": (settings.options_provider if self.providers.mode == "live" else "mock"),
                "tickers": tickers,
                "trades": self.trades.recent(40),
                "summary": self.trades.summary(),
                "server_time": time.time(),
            }
