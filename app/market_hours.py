"""Regular-trading-hours (RTH) gate — shared by both strategies.

These are 0DTE options: all activity is restricted to the US equity regular
session in America/New_York. Entries are blocked outside the session, and any
open position is flattened before the close (a same-day-expiry option must never
be held overnight). Times are configurable via the SESSION_* settings.

Phases:
  pre      before the open                  -> no entries, no positions expected
  open     entries + management allowed
  late     management only, no new entries  (final minutes before flatten)
  flatten  flatten any open 0DTE position
  closed   after the close / weekend        -> no activity
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from typing import Optional

from app.config import settings

log = logging.getLogger("market_hours")

PRE, OPEN, LATE, FLATTEN, CLOSED = "pre", "open", "late", "flatten", "closed"

try:
    from zoneinfo import ZoneInfo
    _ET: Optional["ZoneInfo"] = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata
    _ET = None
    log.warning("zoneinfo America/New_York unavailable; RTH gate uses server local time")


def _parse(hhmm: str, default: dtime) -> dtime:
    try:
        h, m = hhmm.strip().split(":")
        return dtime(int(h), int(m))
    except Exception:
        return default


def now_et() -> datetime:
    return datetime.now(_ET) if _ET else datetime.now()


def phase(dt: Optional[datetime] = None) -> str:
    if not settings.rth_only:
        return OPEN
    dt = dt or now_et()
    if dt.weekday() >= 5:                       # Saturday / Sunday
        return CLOSED
    t = dt.time()
    o = _parse(settings.session_open, dtime(9, 30))
    c = _parse(settings.session_close, dtime(16, 0))
    ne = _parse(settings.session_no_entry, dtime(15, 45))
    fl = _parse(settings.session_flatten, dtime(15, 55))
    if t >= c:
        return CLOSED
    if t < o:
        return PRE
    if t >= fl:
        return FLATTEN
    if t >= ne:
        return LATE
    return OPEN


def entries_allowed(ph: str) -> bool:
    return ph == OPEN


def should_flatten(ph: str) -> bool:
    """Outside the tradeable session a same-day option must be closed out."""
    return ph in (PRE, FLATTEN, CLOSED)


def label(dt: Optional[datetime] = None) -> str:
    dt = dt or now_et()
    tz = dt.strftime("%Z") or ("ET" if _ET else "local")
    return dt.strftime("%H:%M ") + tz


def info(dt: Optional[datetime] = None) -> dict:
    dt = dt or now_et()
    ph = phase(dt)
    return {"phase": ph, "label": label(dt), "open": ph in (OPEN, LATE),
            "rth_only": settings.rth_only}
