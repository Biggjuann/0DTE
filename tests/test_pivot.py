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
        self.price = 740.0          # real (live) underlying
        self.mclose = None          # lagging 1-minute close (None -> tracks price)
        self.vix = 16.06            # > VIX PP (16.0) -> bearish
        self.opt_bid = 3.0
        self.opt_ask = 3.1
        self.opt_last = 3.05
        self.orders = []

    # MarketData
    def get_quote(self, ticker):
        if ticker == "$VIX":
            return Quote(ticker=ticker, last=self.vix, minute_close=self.vix)
        mc = self.mclose if self.mclose is not None else self.price
        return Quote(ticker=ticker, last=self.price, minute_close=mc)

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
    settings.pivot_zones = {"SPY": 0.50, "QQQ": 0.75}   # zone widths (per ticker)
    settings.pivot_stop_pct = 0.5
    settings.pivot_scale_pct = 0.5
    settings.pivot_scale_profit = 0.5       # scale the lot to the runner at +50%
    settings.pivot_runner_qty = 1           # keep 1 runner
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

# --- spike guards (unchanged) ----------------------------------------------
def test_spiked_underlying_does_not_trigger_entry():
    p = FakePivot(); p.price = 787.97
    eng = make_engine(p); eng._tick()
    assert stt(eng).position is None
    assert stt(eng).quote_bad is True
    assert not any(o[0] == "BUY" for o in p.orders)


def test_big_jump_between_ticks_is_rejected():
    p = FakePivot(); p.price = 740.0
    eng = make_engine(p); eng._tick()
    assert stt(eng).last_good_price == 740.0
    p.price = 765.0; eng._tick()
    assert stt(eng).last_good_price == 740.0
    assert stt(eng).quote_bad is True


# --- zone-fade entries -----------------------------------------------------
# QQQ zones (half 0.375): S3 729.52, S2 736.73, S1 741.47, PP 743.94,
#                         R1 748.68, R2 751.15, R3 758.36
def _cross_up_into_s1(eng, p):
    p.price = 740.0; eng._tick()   # below S1 zone (between S2 and S1)
    p.price = 741.2; eng._tick()   # cross UP into S1 zone -> SHORT


def test_short_fade_zone_from_below():
    p = FakePivot(); eng = make_engine(p)
    _cross_up_into_s1(eng, p)
    pos = stt(eng).position
    assert pos and pos.direction == "SHORT" and pos.option_type == "PUT"
    assert pos.entry_zone_label == "S1"
    assert pos.target_label == "S2" and abs(pos.target - 736.73) < 0.05   # next zone DOWN
    assert pos.qty == 4 and pos.remaining_qty == 4
    assert any(o[0] == "BUY" for o in p.orders)


def test_long_fade_zone_from_above():
    p = FakePivot(); eng = make_engine(p)
    p.price = 745.0; eng._tick()   # above PP zone (between PP and R1)
    p.price = 744.0; eng._tick()   # cross DOWN into PP zone -> LONG
    pos = stt(eng).position
    assert pos and pos.direction == "LONG" and pos.option_type == "CALL"
    assert pos.entry_zone_label == "PP"
    assert pos.target_label == "R1" and abs(pos.target - 748.68) < 0.05    # next zone UP


def test_no_entry_when_sitting_in_zone_without_crossing():
    p = FakePivot(); eng = make_engine(p)
    p.price = 741.3; eng._tick()   # first sight already inside S1 zone (no fresh cross)
    assert stt(eng).position is None
    p.price = 741.5; eng._tick()   # still inside, no cross
    assert stt(eng).position is None


def test_no_entry_between_zones():
    p = FakePivot(); eng = make_engine(p)
    p.price = 745.0; eng._tick()   # between PP and R1
    assert stt(eng).position is None
    assert "between zones" in stt(eng).last_event


# --- runner management -----------------------------------------------------
def test_scale_to_runner_at_50pct_profit():
    p = FakePivot(); eng = make_engine(p)
    _cross_up_into_s1(eng, p)
    pos = stt(eng).position; entry = pos.entry_price
    p.opt_bid, p.opt_ask, p.opt_last = entry * 1.6, entry * 1.6 + 0.1, entry * 1.6  # +60%
    eng._tick()
    pos = stt(eng).position
    assert pos.scaled and pos.remaining_qty == 1, "sell 3, keep 1 runner"
    assert pos.breakeven and pos.stop_premium == round(entry, 2)
    sells = [o for o in p.orders if o[0] == "SELL"]
    assert sells and sells[0][2] == 3


def test_stop_disabled_by_default_no_breakeven():
    p = FakePivot(); eng = make_engine(p)
    settings.pivot_stop_pct = 0.0                   # stops OFF (the default)
    _cross_up_into_s1(eng, p)
    pos = stt(eng).position; entry = pos.entry_price
    p.opt_bid, p.opt_ask, p.opt_last = entry * 1.6, entry * 1.6 + 0.1, entry * 1.6
    eng._tick()                                     # scale to runner
    pos = stt(eng).position
    assert pos.scaled and pos.remaining_qty == 1
    assert not pos.breakeven and pos.stop_premium < 0, "no stop when disabled"
    # runner premium collapses toward zero but there is NO stop -> still open
    p.opt_bid, p.opt_ask, p.opt_last = 0.0, 0.0, 0.02
    p.price = 742.5                                 # still above the S2 target zone
    eng._tick()
    assert stt(eng).position is not None, "runner has no stop; only zones/EOD exit it"


def test_runner_exits_at_next_zone():
    p = FakePivot(); eng = make_engine(p)
    _cross_up_into_s1(eng, p)
    pos = stt(eng).position; entry = pos.entry_price
    p.opt_bid, p.opt_ask = entry * 1.6, entry * 1.6 + 0.1
    eng._tick()                                    # scale -> 1 runner
    assert stt(eng).position.remaining_qty == 1
    p.price = 736.8                                # falls into S2 zone (the target)
    p.opt_bid, p.opt_ask = 6.0, 6.1
    eng._tick()
    assert stt(eng).position is None
    assert "runner hit S2" in stt(eng).last_event


def test_min_hold_blocks_immediate_exit():
    p = FakePivot(); eng = make_engine(p)
    settings.pivot_min_hold_seconds = 60.0
    _cross_up_into_s1(eng, p)                       # entry (min-hold doesn't block entry)
    pos = stt(eng).position; entry = pos.entry_price
    p.opt_bid, p.opt_ask = entry * 2, entry * 2 + 0.1
    p.price = 736.8                                # would scale AND hit target
    eng._tick()
    assert stt(eng).position is not None and stt(eng).position.remaining_qty == 4


def test_exit_mark_floored_at_intrinsic():
    p = FakePivot(); eng = make_engine(p)
    _cross_up_into_s1(eng, p)                       # SHORT PUT, ATM strike ~741
    p.opt_bid, p.opt_ask, p.opt_last = 0.0, 0.0, 0.10   # stale one-sided book
    p.price = 736.8                                # put ITM ~4.2; falls into S2 target
    eng._tick()
    sells = [o for o in p.orders if o[0] == "SELL"]
    assert sells and all(o[3] >= 4.0 for o in sells), f"exit below intrinsic: {sells}"


# --- zones / config --------------------------------------------------------
def test_pivots_frozen_for_session():
    from app.clients.pivots import PivotsProvider

    class Src:
        def __init__(self): self.o = {"high": 746.41, "low": 739.2, "close": 746.21}
        def get_prior_day_ohlc(self, t): return self.o

    src = Src()
    pp = PivotsProvider(src)
    a = pp.get_pivots("QQQ")
    src.o = {"high": 800.0, "low": 700.0, "close": 750.0}
    b = pp.get_pivots("QQQ")
    assert b.pp == a.pp and b.r1 == a.r1 and b.s1 == a.s1, "pivots must be frozen for the session"


def test_wide_zone_widens_entry_band():
    p = FakePivot(); eng = make_engine(p)
    settings.pivot_zones = {"QQQ": 4.0}            # half = 2.0; R1 zone [746.68, 750.68]
    p.price = 746.0; eng._tick()                   # below R1 wide zone -> between zones
    p.price = 747.0; eng._tick()                   # 1.68 below R1 line but inside wide zone
    pos = stt(eng).position
    assert pos is not None and pos.entry_zone_label == "R1" and pos.direction == "SHORT"


def test_narrow_zone_no_entry_outside_band():
    p = FakePivot(); eng = make_engine(p)
    settings.pivot_zones = {"QQQ": 0.2}            # half = 0.1; R1 zone [748.58, 748.78]
    p.price = 748.0; eng._tick()
    p.price = 748.4; eng._tick()                   # >0.1 below R1 748.68 -> not in zone
    assert stt(eng).position is None


def test_pivots_use_configured_timeframe():
    from app.clients.pivots import PivotsProvider

    class Src:
        def __init__(self): self.tf = []
        def get_prior_ohlc(self, t, timeframe):
            self.tf.append(timeframe)
            return {"high": 760.0, "low": 740.0, "close": 750.0}

    src = Src()
    pp = PivotsProvider(src, timeframe="weekly")
    lv = pp.get_pivots("QQQ")
    assert src.tf and src.tf[0] == "weekly", "provider must request the weekly period"
    assert lv is not None and lv.pp == round((760 + 740 + 750) / 3, 2)


def test_zone_width_per_ticker_config():
    settings.pivot_zones = {"SPY": 0.50, "QQQ": 0.75}
    assert settings.pivot_zone_width("SPY") == 0.50 and settings.pivot_zone_half("SPY") == 0.25
    assert settings.pivot_zone_width("QQQ") == 0.75 and settings.pivot_zone_half("QQQ") == 0.375
    settings.pivot_proximity = 0.5
    assert settings.pivot_zone_width("IWM") == 1.0


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
