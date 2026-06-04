"""State-machine tests using a scripted provider (no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.base import Fill
from app.clients.factory import Providers
from app.config import settings
from app.models import (Levels, MMSignal, MMStatus, OptionContract,
                        PositionState, Quote)
from app.strategy.engine import Engine


class ScriptProvider:
    """Hand-driven provider; mutate attributes between ticks to script scenarios."""

    def __init__(self):
        self.levels = Levels(ticker="QQQ", lower=525.0, mid=532.0, top=540.0,
                             gvwap=532.0, lowest_put=525.0, highest_call=540.0)
        self.status = MMStatus.LONG
        self.puts_below = 1_400_000
        self.calls_above = 900_000
        self.price = 530.0
        self.orders = []

    # LevelsProvider
    def get_levels(self, ticker): return self.levels
    # MMProvider
    def get_signal(self, ticker):
        return MMSignal(ticker=ticker, status=self.status,
                        puts_below_spot=self.puts_below, calls_at_above_spot=self.calls_above)
    # MarketData
    def get_quote(self, ticker): return Quote(ticker=ticker, last=self.price, minute_close=self.price)
    def get_0dte_calls(self, ticker, near_strike, width=5.0):
        return [OptionContract(symbol=f"{ticker}_C{int(near_strike)}", strike=float(int(near_strike)),
                               expiry="0dte", bid=1.2, ask=1.3, last=1.25)]
    def get_contract(self, symbol):
        # premium tracks the underlying for P&L realism
        strike = float(symbol.split("C")[-1])
        prem = max(0.0, self.price - strike) + 1.0
        return OptionContract(symbol=symbol, strike=strike, expiry="0dte",
                              bid=prem - .05, ask=prem + .05, last=prem)
    # Broker
    def buy_to_open_call(self, contract, qty):
        self.orders.append(("BUY", contract.symbol, qty)); return Fill(True, contract.ask, "ok")
    def sell_to_close_call(self, contract, qty):
        self.orders.append(("SELL", contract.symbol, qty)); return Fill(True, contract.bid, "ok")


def make_engine(p):
    settings.tickers = ["QQQ"]
    settings.rth_only = False   # mechanics tests run regardless of wall-clock time
    settings.proximity = 1.0
    settings.contracts = 1
    settings.strike_offset = 1.0
    # Re-poll every tick so scripted state changes are picked up immediately.
    settings.levels_poll_seconds = 0
    settings.mm_poll_seconds = 0
    settings.quote_poll_seconds = 0
    eng = Engine(providers=Providers(levels=p, mm=p, market=p, broker=p, mode="test"))
    # Isolate the trade log to a throwaway temp file so tests never touch the
    # app's real data/trades.json.
    import tempfile
    from app.store import TradeLog
    eng.trades = TradeLog(path=tempfile.mktemp(suffix=".json"))
    return eng


def st(eng): return eng.states["QQQ"]


def test_no_entry_without_approval():
    p = ScriptProvider(); p.status = MMStatus.NEUTRAL; p.price = 525.0
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.DISABLED
    assert st(eng).position is None


def test_no_entry_until_near_lower():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 530.0  # not within $1 of 525
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.ARMED
    assert st(eng).position is None


def test_entry_at_putwall_when_long():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.4  # within $1 of put wall 525
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    assert st(eng).position.strike == 533.0  # strike == mid (532) + $1 offset
    assert ("BUY", "QQQ_C533", 1) in p.orders


def test_entry_at_putwall_when_cautious_long():
    p = ScriptProvider(); p.status = MMStatus.CAUTIOUS_LONG; p.price = 525.2
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN  # cautious-long also approves


def test_no_entry_when_price_above_putwall_band():
    # The exact bug: live price well above the put wall must NOT enter.
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 528.0  # >$1 from 525
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.ARMED
    assert st(eng).position is None


def test_enters_on_tight_channel_when_ordered():
    # Tight but properly-ordered channel (mid just above put wall) should still
    # enter on cautious-long and NOT instantly exit (no downgrade yet).
    p = ScriptProvider(); p.status = MMStatus.CAUTIOUS_LONG; p.price = 745.2
    p.levels.lower = 745.0; p.levels.mid = 745.34; p.levels.top = 750.0
    eng = make_engine(p); eng._tick(); eng._tick()
    assert st(eng).state is PositionState.OPEN
    assert st(eng).position is not None


def test_no_entry_on_inverted_channel():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.0
    p.levels.top = 520.0  # top below mid -> inverted, untradeable
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.DISABLED
    assert st(eng).position is None


def test_no_mid_exit_when_entered_cautious_no_downgrade():
    # Entered on cautious-long (never long): mid exit must NOT fire even at mid.
    p = ScriptProvider(); p.status = MMStatus.CAUTIOUS_LONG; p.price = 525.2
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    p.price = 531.8  # at/above mid (532) while still cautious, no downgrade
    eng._tick()
    assert st(eng).position is not None, "no downgrade from long -> must hold"


def test_no_reentry_churn_same_touch():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.4
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    # Force a flat state without leaving the put-wall band, then re-tick.
    eng._exit(st(eng), "test flatten", "TEST")
    n = len([o for o in p.orders if o[0] == "BUY"])
    p.price = 525.5  # still within the band, never left
    eng._tick(); eng._tick()
    assert len([o for o in p.orders if o[0] == "BUY"]) == n, "must not re-enter same touch"
    # Price leaves the band and re-touches -> one new entry allowed.
    p.price = 530.0; eng._tick()   # leaves band -> re-arm
    p.price = 525.3; eng._tick()   # re-touch -> entry
    assert len([o for o in p.orders if o[0] == "BUY"]) == n + 1


def test_exit_at_mid_on_downgrade():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.2
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    # Price climbs to mid and status downgrades -> exit at mid
    p.price = 531.8; p.status = MMStatus.CAUTIOUS_LONG
    eng._tick()
    assert st(eng).position is None
    assert any(o[0] == "SELL" for o in p.orders)


def test_hold_to_top_while_long():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.0
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    # At mid but STILL long -> must NOT exit
    p.price = 532.0
    eng._tick()
    assert st(eng).state is PositionState.OPEN, "should hold through mid while LONG"
    # Reaches top while long -> exit at top
    p.price = 539.6
    eng._tick()
    assert st(eng).position is None
    assert any(o[0] == "SELL" for o in p.orders)


def test_stop_when_close_below_lower():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.0  # near lower
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    # 1-min close drops below the lower level (525) while still long -> STOP
    p.price = 524.0
    eng._tick()
    assert st(eng).position is None
    assert any(o[0] == "SELL" for o in p.orders)
    assert "STOP" in st(eng).last_event


def test_top_exit_fires_when_in_top_zone():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.0
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    # price reaches the top zone (within $1 of top 540) while long -> exit at top
    p.price = 539.2
    eng._tick()
    assert st(eng).position is None
    assert "top" in st(eng).last_event.lower()


def test_strike_picks_closest_available_not_nearest_dollar():
    p = ScriptProvider()
    # A real strike ladder including a fractional strike closer to the target.
    p.get_0dte_calls = lambda t, near_strike, width=5.0: [
        OptionContract("A", 746.0, "0dte", 1.0, 1.1, 1.05),
        OptionContract("B", 746.5, "0dte", 1.0, 1.1, 1.05),
        OptionContract("C", 747.0, "0dte", 1.0, 1.1, 1.05),
    ]
    eng = make_engine(p)
    c = eng._pick_call("QQQ", 746.66)   # target mid+offset
    assert c.strike == 746.5, "must pick the closest listed strike, not the nearest dollar"


def test_protective_exit_on_bearish():
    p = ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.0
    eng = make_engine(p); eng._tick()
    assert st(eng).state is PositionState.OPEN
    p.status = MMStatus.SHORT; p.price = 528.0
    eng._tick()
    assert st(eng).position is None
    assert st(eng).state is PositionState.DISABLED


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
