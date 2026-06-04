"""Pivot-engine bad-data guard tests (the incident reproduction).

The blowup: a single spiked underlying print (787.97 vs real ~740) tripped a
SHORT entry, then a corrected next tick scaled + targeted out in the same second,
and a stale option mid (0.51 on a deep-ITM put) booked a fake −$996. These tests
prove the four guards now prevent each step.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.base import Fill
from app.clients.factory import PivotProviders
from app.config import settings
from app.models import OptionContract, PivotLevels, Quote
from app.strategy.pivot_engine import PivotEngine
from app.store import TradeLog


class FakePivot:
    """Scriptable provider: set .price / .vix / .opt_bid / .opt_ask between ticks."""

    def __init__(self):
        # Prior-day OHLC chosen so PP≈743.94, R1≈748.66, S1≈741.45 (the incident).
        self.ohlc = {"QQQ": {"high": 746.41, "low": 739.2, "close": 746.21},
                     "$VIX": {"high": 17.0, "low": 15.0, "close": 16.0}}
        self.price = 740.0          # real underlying
        self.vix = 16.06            # > VIX PP (16.0) -> bearish
        self.opt_bid = 3.0
        self.opt_ask = 3.1
        self.opt_last = 3.05
        self.orders = []

    # MarketData
    def get_quote(self, ticker):
        v = self.vix if ticker == "$VIX" else self.price
        return Quote(ticker=ticker, last=v, minute_close=v)

    def get_prior_day_ohlc(self, ticker):
        return self.ohlc.get(ticker)

    def _chain(self, ticker, near, cp):
        k = round(near)
        return [OptionContract(symbol=f"{ticker}_0DTE_{cp}{k}", strike=float(k), expiry="0dte",
                               bid=self.opt_bid, ask=self.opt_ask, last=self.opt_last)]

    def get_0dte_puts(self, ticker, near_strike, width=5.0): return self._chain(ticker, near_strike, "P")
    def get_0dte_calls(self, ticker, near_strike, width=5.0): return self._chain(ticker, near_strike, "C")

    def get_contract(self, symbol):
        return OptionContract(symbol=symbol, strike=744.0, expiry="0dte",
                              bid=self.opt_bid, ask=self.opt_ask, last=self.opt_last)

    # Broker
    def buy_to_open(self, contract, qty):
        self.orders.append(("BUY", contract.symbol, qty, contract.ask))
        return Fill(True, contract.ask or contract.mid, "ok")

    def sell_to_close(self, contract, qty):
        self.orders.append(("SELL", contract.symbol, qty, contract.bid))
        return Fill(True, contract.bid or contract.mid, "ok")


def make_engine(p):
    settings.tickers = ["QQQ"]
    settings.rth_only = False   # guard tests run regardless of wall-clock time
    settings.pivot_contracts = 4
    settings.pivot_proximity = 0.5
    settings.pivot_stop_pct = 0.5
    settings.pivot_scale_pct = 0.5
    settings.pivot_min_hold_seconds = 0.0   # off unless a test enables it
    settings.pivot_max_dev_pct = 0.03
    settings.pivot_max_jump_pct = 0.02
    settings.quote_poll_seconds = 0
    settings.mm_poll_seconds = 0
    settings.levels_poll_seconds = 0
    eng = PivotEngine(providers=PivotProviders(pivots=_PivotsP(p), market=p, broker=p,
                                               vix_symbol="$VIX", mode="test"))
    eng.trades = TradeLog(path=tempfile.mktemp(suffix=".json"))
    return eng


class _PivotsP:
    def __init__(self, src): self.src = src
    def get_pivots(self, ticker):
        o = self.src.get_prior_day_ohlc(ticker)
        return PivotLevels.from_ohlc(ticker, o["high"], o["low"], o["close"]) if o else None


def stt(eng): return eng.states["QQQ"]


# --- the guards ------------------------------------------------------------
def test_spiked_underlying_does_not_trigger_entry():
    """787.97 (≈6% above prior close) must be rejected, not traded."""
    p = FakePivot(); p.price = 787.97
    eng = make_engine(p); eng._tick()
    assert stt(eng).position is None, "spiked print must not open a position"
    assert stt(eng).quote_bad is True
    assert not any(o[0] == "BUY" for o in p.orders)


def test_big_jump_between_ticks_is_rejected():
    p = FakePivot(); p.price = 740.0
    eng = make_engine(p); eng._tick()                 # establish a good baseline
    assert stt(eng).last_good_price == 740.0
    p.price = 765.0                                   # +3.4% jump in one tick
    eng._tick()
    assert stt(eng).last_good_price == 740.0, "jump should be rejected, baseline held"
    assert stt(eng).quote_bad is True


def test_clean_quote_at_r1_enters_short():
    p = FakePivot(); p.price = 748.5                  # at R1 (748.66) within prox, ~0.4% dev OK?
    # 748.5 vs prior close 746.21 is 0.31% — within the 3% band, so accepted.
    eng = make_engine(p); eng._tick()
    assert stt(eng).position is not None
    assert stt(eng).position.direction == "SHORT"
    assert stt(eng).position.option_type == "PUT"


def test_min_hold_blocks_same_tick_exit():
    p = FakePivot(); p.price = 748.5
    eng = make_engine(p)
    settings.pivot_min_hold_seconds = 60.0            # cannot exit immediately
    eng._tick()
    assert stt(eng).position is not None
    p.price = 740.0                                   # would otherwise scale+target
    eng._tick()
    assert stt(eng).position is not None, "min-hold must keep the fresh position open"
    assert stt(eng).position.remaining_qty == 4


def test_exit_mark_floored_at_intrinsic():
    """A stale 0.51 mid on a deep-ITM put must not fabricate a loss."""
    p = FakePivot(); p.price = 748.5
    eng = make_engine(p); eng._tick()
    pos = stt(eng).position
    # Stale/one-sided option quote, far below intrinsic.
    p.opt_bid, p.opt_ask, p.opt_last = 0.0, 0.0, 0.51
    p.price = 740.0                                   # 744 put now ITM ~4
    eng._tick()
    # Whatever closed, no leg should have filled below intrinsic (~4 -> >= entry).
    sells = [o for o in p.orders if o[0] == "SELL"]
    assert sells, "should have scaled/closed"
    assert all(o[3] >= 3.9 for o in sells), f"exit filled below intrinsic: {sells}"


def test_one_action_per_tick_scale_then_target():
    """Scale and final target must not both fire on the same tick."""
    p = FakePivot(); p.price = 748.5
    eng = make_engine(p); eng._tick()                 # entry
    # premium tracks intrinsic so no premium-stop interferes
    p.opt_bid, p.opt_ask, p.opt_last = 6.0, 6.1, 6.05
    p.price = 740.0                                   # gaps below PP and S1 at once
    eng._tick()
    pos = stt(eng).position
    assert pos is not None and pos.scaled and pos.remaining_qty == 2, "scaled only this tick"
    eng._tick()                                       # now the runner targets out
    assert stt(eng).position is None


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS  {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL  {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
