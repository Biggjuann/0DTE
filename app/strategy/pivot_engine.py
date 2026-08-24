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

from app import market_hours
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
    last_good_price: Optional[float] = None   # last validated decision price
    prev_price: Optional[float] = None        # price on the previous tick (for zone crossing)
    quote_bad: bool = False          # currently rejecting spiked quotes
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
                    self._last_quote[ticker] = now
                    if q is not None:
                        self._accept_quote(st, q)
                self._evaluate(st)
                st.updated = now

    def _accept_quote(self, st: PivotTickerState, q: Quote) -> None:
        """Validate the live price and reject single spiked/stale prints.

        Decisions use the LIVE `last` (not the 1-minute close): the dev/jump
        guards below provide the spike protection, while the minute close lagged
        the market and made scale/target fire late or not at all."""
        dp = q.last or q.minute_close
        if dp is None:
            return
        reason = None
        if st.last_good_price:
            jump = abs(dp - st.last_good_price) / st.last_good_price
            if jump > settings.pivot_max_jump_pct:
                reason = f"jump {jump*100:.1f}% from {st.last_good_price:.2f}"
        pc = st.pivots.prior_close if st.pivots else None
        if reason is None and pc:
            dev = abs(dp - pc) / pc
            if dev > settings.pivot_max_dev_pct:
                reason = f"dev {dev*100:.1f}% from prior close {pc:.2f}"
        if reason is not None:
            log.warning("[%s] quote rejected: %.2f (%s)", st.ticker, dp, reason)
            if not st.quote_bad:
                st.log_event(f"QUOTE REJECTED {dp:.2f} ({reason}) — holding {st.last_good_price}")
            st.quote_bad = True
            return
        if st.quote_bad:
            st.log_event(f"quote recovered @ {dp:.2f}")
        st.quote_bad = False
        st.quote = q
        st.last_good_price = dp

    # --- state machine -------------------------------------------------------
    def _evaluate(self, st: PivotTickerState) -> None:
        # RTH gate: 0DTE trades only during the regular session. Outside it,
        # flatten any open position and never enter.
        ph = market_hours.phase()
        if market_hours.should_flatten(ph):
            if st.position:
                why = "session close" if ph == market_hours.FLATTEN else "outside RTH"
                self._close(st, st.position.remaining_qty, "EOD",
                            f"{why} — flatten 0DTE ({market_hours.label()})")
            if not st.position:
                self._session_idle(st)
            return

        if not (st.pivots and st.quote and st.last_good_price):
            return
        pv, price = st.pivots, st.last_good_price   # validated live price
        half = settings.pivot_zone_half(st.ticker)  # each level is a zone ±half wide
        st.pivots.spot = st.quote.last
        entries_open = market_hours.entries_allowed(ph)  # False during 'late'
        prev = st.prev_price if st.prev_price is not None else price
        try:
            if st.position is None:
                self._look_for_zone_entry(st, pv, price, prev, half, entries_open)
            else:
                self._manage(st)
        finally:
            st.prev_price = price   # remember for next tick's crossing check

    def _zones(self, pv: PivotLevels):
        """The 7 pivot levels, ascending, as (value, label)."""
        return sorted([(pv.s3, "S3"), (pv.s2, "S2"), (pv.s1, "S1"), (pv.pp, "PP"),
                       (pv.r1, "R1"), (pv.r2, "R2"), (pv.r3, "R3")], key=lambda z: z[0])

    def _look_for_zone_entry(self, st, pv, price, prev, half, entries_open):
        zones = self._zones(pv)
        # Which zone is price currently inside? (nearest line if several overlap)
        inside = [(i, v, lab) for i, (v, lab) in enumerate(zones)
                  if v - half <= price <= v + half]
        if not inside:
            self._arm(st, f"price {price:.2f} between zones — waiting for a zone touch")
            return
        i, lvl, lab = min(inside, key=lambda z: abs(price - z[1]))
        # Direction is set by how we ENTERED the zone this tick.
        if prev < lvl - half:            # came up into the zone from below -> fade SHORT (puts)
            direction, ot = "SHORT", "PUT"
            tgt = zones[i - 1] if i > 0 else None            # runner exits at next zone DOWN
        elif prev > lvl + half:          # came down into the zone from above -> fade LONG (calls)
            direction, ot = "LONG", "CALL"
            tgt = zones[i + 1] if i < len(zones) - 1 else None  # runner exits at next zone UP
        else:
            # already sitting in the zone (no fresh crossing) — do not re-enter
            self._arm(st, f"in {lab} zone {lvl:.2f} (no fresh touch) — standing by")
            return
        if not entries_open:
            self._arm(st, f"{direction} touch at {lab} but late session "
                          f"({market_hours.label()}) — no new entries")
            return
        self._enter(st, direction, ot, lvl, lab, tgt)

    def _arm(self, st: PivotTickerState, why: str) -> None:
        if st.state != "armed":
            st.log_event(f"ARMED — {why}")
        st.state = "armed"

    def _session_idle(self, st: PivotTickerState) -> None:
        if st.state != "closed":
            st.log_event(f"market closed ({market_hours.label()}) — idle")
        st.state = "closed"

    # --- entries -------------------------------------------------------------
    def _pick(self, ticker: str, option_type: str, target: float) -> Optional[OptionContract]:
        chain = (self.providers.market.get_0dte_puts(ticker, near_strike=target)
                 if option_type == "PUT"
                 else self.providers.market.get_0dte_calls(ticker, near_strike=target))
        if not chain:
            return None
        return min(chain, key=lambda c: abs(c.strike - target))

    def _enter(self, st: PivotTickerState, direction: str, option_type: str,
               level: float, level_label: str, target) -> None:
        under = st.last_good_price
        tgt_val = target[0] if target else None
        tgt_lab = target[1] if target else "—"
        approach = "from below" if direction == "SHORT" else "from above"
        if not self.auto_trade:
            st.log_event(f"{direction} touch {level_label} {level:.2f} ({approach}) "
                         f"(auto-trade OFF — not sent)")
            st.state = "armed"
            return
        # ATM: strike closest to the current underlying.
        contract = self._pick(st.ticker, option_type, under)
        if not contract:
            st.log_event("ENTRY blocked — no 0DTE chain")
            st.state = "armed"
            return
        if contract.bid <= 0 or contract.ask <= 0:
            st.log_event(f"ENTRY blocked — one-sided/stale market on {contract.symbol} "
                         f"(bid {contract.bid:.2f} / ask {contract.ask:.2f})")
            st.state = "armed"
            return
        fill = self.providers.broker.buy_to_open(contract, settings.pivot_contracts)
        if not fill.accepted:
            st.log_event(f"ENTRY rejected: {fill.message}")
            st.state = "armed"
            return
        qty = settings.pivot_contracts
        stop = round(fill.price * (1 - settings.pivot_stop_pct), 2) if settings.pivot_stop_pct > 0 else -1.0
        st.position = PivotPosition(
            ticker=st.ticker, direction=direction, option_type=option_type,
            contract_symbol=contract.symbol, strike=contract.strike, expiry=contract.expiry,
            qty=qty, remaining_qty=qty, entry_price=fill.price, entry_time=time.time(),
            entry_underlying=under, pp=level, target=tgt_val,
            entry_zone_label=level_label, target_label=tgt_lab,
            current_price=fill.price, last_underlying=under, stop_premium=stop,
        )
        st.state = "open"
        st.can_enter = False
        runner = f"runner → {tgt_lab} {tgt_val:.2f}" if tgt_val is not None else "runner → no zone (stop/EOD)"
        st.log_event(f"ENTRY {direction} {qty}x {contract.symbol} @ {fill.price:.2f} "
                     f"(ATM strike {contract.strike:g}; fade {level_label} {level:.2f} {approach}; "
                     f"{runner})")
        self.trades.append(self._record("ENTRY", st.position, qty, fill.price, None, None))

    # --- management ----------------------------------------------------------
    def _intrinsic(self, pos: PivotPosition, underlying: float) -> float:
        return (max(0.0, pos.strike - underlying) if pos.option_type == "PUT"
                else max(0.0, underlying - pos.strike))

    def _mark(self, pos: PivotPosition, c: Optional[OptionContract], underlying: float) -> float:
        """A premium mark that is never below intrinsic, so a stale one-sided
        quote (e.g. 0.51 on a deep-ITM option) can't fabricate a loss."""
        intrinsic = round(self._intrinsic(pos, underlying), 2)
        est = c.mid if (c and c.bid > 0 and c.ask > 0) else (c.mid if c else 0.0)
        return round(max(est, intrinsic), 2)

    def _manage(self, st: PivotTickerState) -> None:
        pos = st.position
        under = st.last_good_price
        pos.last_underlying = under
        c = self.providers.market.get_contract(pos.contract_symbol)
        pos.current_price = self._mark(pos, c, under)
        price = under
        half = settings.pivot_zone_half(st.ticker)   # levels are zones ±half wide

        # 0) Let a fresh fill breathe — never enter and fully exit on one spike.
        if time.time() - pos.entry_time < settings.pivot_min_hold_seconds:
            return

        # 1) Premium stop (disabled if <0; breakeven after the scale).
        if pos.stop_premium >= 0 and pos.current_price <= pos.stop_premium:
            label = "breakeven stop" if pos.breakeven else "premium stop"
            self._close(st, pos.remaining_qty, "STOP", f"{label} @ {pos.current_price:.2f}")
            return

        # 2) Scale to the runner at +PIVOT_SCALE_PROFIT (default +50%): sell all
        #    but PIVOT_RUNNER_CONTRACTS, then move the stop to breakeven.
        scale_at = round(pos.entry_price * (1 + settings.pivot_scale_profit), 2)
        if not pos.scaled and pos.current_price >= scale_at:
            take = max(0, pos.remaining_qty - max(1, settings.pivot_runner_qty))
            if take > 0:
                self._close(st, take, "SCALE",
                            f"+{settings.pivot_scale_profit*100:.0f}% @ {pos.current_price:.2f} "
                            f"— {take} off, {pos.remaining_qty-take} runner", keep_open=True)
            pos.scaled = True
            if settings.pivot_stop_pct > 0:            # breakeven only if stops are on
                pos.breakeven = True
                pos.stop_premium = round(pos.entry_price, 2)
                st.log_event(f"stop moved to breakeven {pos.stop_premium:.2f}")
            if pos.remaining_qty <= 0:
                st.position = None
                st.state = "armed"
            return

        # 3) Runner exits when price reaches the NEXT zone (down for shorts, up
        #    for longs). No next zone -> runner rides to the stop / EOD flatten.
        if pos.target is not None:
            hit = (price <= pos.target + half) if pos.direction == "SHORT" else (price >= pos.target - half)
            if hit:
                self._close(st, pos.remaining_qty, "TARGET",
                            f"runner hit {pos.target_label} zone {pos.target:.2f}")
                return

    def _close(self, st: PivotTickerState, qty: int, exit_type: str, reason: str,
               keep_open: bool = False) -> None:
        pos = st.position
        if not pos or qty <= 0:
            return
        # Price the exit off an intrinsic-floored mark so a stale book can't
        # fill at an impossible premium (dry-run *or* live limit price).
        under = st.last_good_price if st.last_good_price is not None else pos.last_underlying
        mark = self._mark(pos, self.providers.market.get_contract(pos.contract_symbol), under)
        contract = OptionContract(symbol=pos.contract_symbol, strike=pos.strike,
                                  expiry=pos.expiry, bid=mark, ask=mark, last=mark)
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
            reason=reason or f"{pos.direction} fade {pos.entry_zone_label} ({pos.option_type})",
            underlying=pos.last_underlying, contract_symbol=pos.contract_symbol,
            strike=pos.strike, qty=qty, price=price, pnl=pnl, dry_run=settings.dry_run,
            stance=f"fade {pos.entry_zone_label}", entry_price=pos.entry_price,
            entry_underlying=pos.entry_underlying,
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
                    "zone_width": settings.pivot_zone_width(st.ticker),
                    "pivots": to_jsonable(st.pivots) if st.pivots else None,
                    "quote": to_jsonable(st.quote) if st.quote else None,
                    "direction": pos.direction if pos else None,
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
                "timeframe": settings.pivot_timeframe,
                "session": market_hours.info(),
                "vix_symbol": self.providers.vix_symbol,
                "vix_last": self.vix_last,
                "vix_pp": self.vix_pp,
                "regime": self.regime,
                "tickers": tickers,
                "trades": self.trades.recent(40),
                "summary": self.trades.summary(),
                "server_time": time.time(),
            }
