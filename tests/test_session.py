"""RTH gate tests for BOTH engines (the overnight/pre-market blowups).

Gamma 0DTE churned pre-market (8 enter/stop round-trips ~6:36-8:30 AM ET) and the
pivot engine opened LONG calls at midnight/4 AM and held to the open. With the
RTH gate, neither engine enters outside the regular session, and any open 0DTE is
flattened before the close.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import market_hours as mh
from app.config import settings
from app.models import MMStatus, PositionState

import tests.test_strategy as G   # gamma scaffolding (ScriptProvider, make_engine)
import tests.test_pivot as P      # pivot scaffolding (FakePivot, make_engine)


def _wed(h, m=0):
    return datetime(2026, 6, 3, h, m)   # 2026-06-03 is a Wednesday


# ---- phase logic ----------------------------------------------------------
def test_phase_premarket():
    settings.rth_only = True
    assert mh.phase(_wed(8, 0)) == mh.PRE


def test_phase_open():
    settings.rth_only = True
    assert mh.phase(_wed(10, 0)) == mh.OPEN


def test_phase_late_no_entry():
    settings.rth_only = True
    assert mh.phase(_wed(15, 50)) == mh.LATE
    assert mh.entries_allowed(mh.phase(_wed(15, 50))) is False


def test_phase_flatten():
    settings.rth_only = True
    assert mh.phase(_wed(15, 57)) == mh.FLATTEN
    assert mh.should_flatten(mh.phase(_wed(15, 57))) is True


def test_phase_closed_evening():
    settings.rth_only = True
    assert mh.phase(_wed(17, 0)) == mh.CLOSED


def test_phase_weekend():
    settings.rth_only = True
    assert mh.phase(datetime(2026, 6, 6, 11, 0)) == mh.CLOSED   # Saturday


def test_rth_disabled_always_open():
    settings.rth_only = False
    assert mh.phase(_wed(3, 0)) == mh.OPEN
    settings.rth_only = True


# ---- gamma engine ---------------------------------------------------------
def test_gamma_no_entry_premarket():
    settings.rth_only = True
    mh.now_et = lambda: _wed(8, 0)                  # pre-market
    p = G.ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.4  # at put wall
    eng = G.make_engine(p)
    settings.rth_only = True                        # G.make_engine sets it False
    eng._tick()
    assert G.st(eng).position is None, "no entry pre-market"
    assert G.st(eng).state is PositionState.DISABLED
    assert "closed" in G.st(eng).last_event.lower()


def test_gamma_flatten_before_close():
    settings.rth_only = True
    mh.now_et = lambda: _wed(10, 0)                 # open -> enter
    p = G.ScriptProvider(); p.status = MMStatus.LONG; p.price = 525.4
    eng = G.make_engine(p); settings.rth_only = True
    eng._tick()
    assert G.st(eng).position is not None, "should be open during RTH"
    mh.now_et = lambda: _wed(15, 57)               # flatten window
    eng._tick()
    assert G.st(eng).position is None, "0DTE must be flattened before the close"
    assert any(o[0] == "SELL" for o in p.orders)
    mh.now_et = mh._orig_now if hasattr(mh, "_orig_now") else mh.now_et


# ---- pivot engine ---------------------------------------------------------
def test_pivot_no_entry_overnight():
    settings.rth_only = True
    mh.now_et = lambda: _wed(4, 0)                  # 4 AM, like the incident
    p = P.FakePivot(); p.price = 748.5              # bearish, at R1 -> would short
    eng = P.make_engine(p); settings.rth_only = True
    eng._tick()
    assert P.stt(eng).position is None, "no entry overnight"
    assert P.stt(eng).state == "closed"
    assert not any(o[0] == "BUY" for o in p.orders)


def test_pivot_flatten_before_close():
    settings.rth_only = True
    mh.now_et = lambda: _wed(10, 0)                 # open -> enter short
    p = P.FakePivot()
    eng = P.make_engine(p); settings.rth_only = True
    p.price = 747.5; eng._tick()                    # below R1 zone
    p.price = 748.5; eng._tick()                    # cross up into R1 zone -> SHORT
    assert P.stt(eng).position is not None
    mh.now_et = lambda: _wed(15, 57)               # flatten window
    eng._tick()
    assert P.stt(eng).position is None, "0DTE must be flattened before the close"
    assert any(o[0] == "SELL" for o in p.orders)


def _restore():
    # leave the module clock back on the real one for any later imports
    import importlib
    importlib.reload(mh)


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS  {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL  {fn.__name__}"); traceback.print_exc()
    settings.rth_only = True
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
