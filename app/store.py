"""Lightweight persistence for the trade log + daily P&L.

Stored as JSON at TRADE_LOG_PATH (point it at a mounted Railway Volume to
survive redeploys). Exposes CSV export + analytics for offline review.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import threading
from typing import Dict, List

from app.config import settings
from app.models import TRADE_CSV_FIELDS, TradeRecord, to_jsonable

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "data", "trades.json")


class TradeLog:
    def __init__(self, path: str | None = None):
        self.path = path or settings.trade_log_path or DEFAULT_PATH
        self._lock = threading.Lock()
        self._trades: List[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r") as fh:
                self._trades = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            self._trades = []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._trades, fh, indent=2)
        os.replace(tmp, self.path)

    def append(self, rec: TradeRecord) -> None:
        with self._lock:
            self._trades.append(to_jsonable(rec))
            self._save()

    def all(self) -> List[dict]:
        with self._lock:
            return list(self._trades)

    def recent(self, limit: int = 50) -> List[dict]:
        with self._lock:
            return list(reversed(self._trades[-limit:]))

    def to_csv(self) -> str:
        rows = self.all()
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=TRADE_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for t in rows:
            row = dict(t)
            row["datetime"] = dt.datetime.fromtimestamp(t["ts"]).isoformat() if t.get("ts") else ""
            w.writerow(row)
        return buf.getvalue()

    def summary(self) -> Dict[str, float]:
        with self._lock:
            exits = [t for t in self._trades if t.get("action") == "EXIT" and t.get("pnl") is not None]
        realized = round(sum(t["pnl"] for t in exits), 2)
        wins = [t for t in exits if t["pnl"] > 0]
        losses = [t for t in exits if t["pnl"] <= 0]
        today = dt.date.today().isoformat()
        today_pnl = round(sum(t["pnl"] for t in exits
                              if dt.datetime.fromtimestamp(t["ts"]).date().isoformat() == today), 2)
        # Exit-type breakdown for review.
        by_type: Dict[str, int] = {}
        for t in exits:
            by_type[t.get("exit_type") or "?"] = by_type.get(t.get("exit_type") or "?", 0) + 1
        holds = [t["hold_seconds"] for t in exits if t.get("hold_seconds")]
        return {
            "realized_pnl": realized,
            "today_pnl": today_pnl,
            "closed_trades": len(exits),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(exits) * 100, 1) if exits else 0.0,
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
            "avg_hold_min": round(sum(holds) / len(holds) / 60, 1) if holds else 0.0,
            "exit_types": by_type,
        }
