"""Lightweight persistence for the trade log + daily P&L."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from typing import Dict, List

from app.models import TradeRecord, to_jsonable

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
TRADES_PATH = os.path.join(DATA_DIR, "trades.json")


class TradeLog:
    def __init__(self, path: str = TRADES_PATH):
        self.path = path
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

    def recent(self, limit: int = 50) -> List[dict]:
        with self._lock:
            return list(reversed(self._trades[-limit:]))

    def summary(self) -> Dict[str, float]:
        """Realised P&L stats across closed trades."""
        with self._lock:
            exits = [t for t in self._trades if t.get("action") == "EXIT" and t.get("pnl") is not None]
        realized = round(sum(t["pnl"] for t in exits), 2)
        wins = [t for t in exits if t["pnl"] > 0]
        today = dt.date.today().isoformat()
        today_pnl = round(sum(t["pnl"] for t in exits
                              if dt.datetime.fromtimestamp(t["ts"]).date().isoformat() == today), 2)
        return {
            "realized_pnl": realized,
            "today_pnl": today_pnl,
            "closed_trades": len(exits),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(exits) * 100, 1) if exits else 0.0,
        }
