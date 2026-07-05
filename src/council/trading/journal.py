"""Trade journal — the council's memory. SQLite store of every decision, its marks, and its
true outcome once the market resolves. Pure + clock-injected for deterministic tests."""
from __future__ import annotations

import datetime
import sqlite3
import threading
import time
from typing import Callable

from .calibrate import brier


def _series(market_id: str) -> str:
    return market_id.split("-")[0]


def _synchronized(fn):
    """Serialize DB access — the journal is touched from the floor's worker tick thread."""
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return wrapper


class Journal:
    _EDGE_BUCKETS = [("<5c", 0.0, 0.05), ("5-15c", 0.05, 0.15),
                     ("15-25c", 0.15, 0.25), (">25c", 0.25, 9.0)]

    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        # The floor ticks in a worker thread (asyncio.to_thread) while it's built on the event
        # loop — so the connection MUST allow cross-thread use, serialized by a lock.
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, opened_ts REAL, market_id TEXT, series TEXT,
            title TEXT, side TEXT, converged_p REAL, blind_p REAL, spread REAL, market_price REAL,
            executable_price REAL, edge REAL, contracts INTEGER, fill_price REAL, fee REAL,
            fill_count INTEGER, decision_reason TEXT, rationale TEXT, status TEXT,
            last_mark_price REAL, last_mark_ts REAL, unrealized_pnl REAL,
            outcome TEXT, realized_pnl REAL, council_correct INTEGER, resolved_ts REAL)""")
        try:                                   # migrate pre-existing journals in place
            self._c.execute("ALTER TABLE trades ADD COLUMN blind_p REAL")
        except sqlite3.OperationalError:
            pass                               # column already exists
        self._c.commit()

    @_synchronized
    def log(self, *, market_id, title, side, converged_p, spread, market_price,
            executable_price, edge, contracts, fill_price, fee, fill_count,
            decision_reason, rationale, status="placed", blind_p=None) -> int:
        cur = self._c.execute(
            """INSERT INTO trades(opened_ts,market_id,series,title,side,converged_p,blind_p,spread,
               market_price,executable_price,edge,contracts,fill_price,fee,fill_count,
               decision_reason,rationale,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.clock(), market_id, _series(market_id), title, side, converged_p, blind_p, spread,
             market_price, executable_price, edge, contracts, fill_price, fee, fill_count,
             decision_reason, rationale, status))
        self._c.commit()
        return cur.lastrowid

    @_synchronized
    def get(self, trade_id: int) -> dict | None:
        r = self._c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(r) if r else None

    @_synchronized
    def mark(self, market_id: str, yes_price: float) -> None:
        for r in self._c.execute(
                "SELECT id,side,fill_price,contracts FROM trades WHERE market_id=? AND status='placed'",
                (market_id,)).fetchall():
            cur_val = yes_price if r["side"] == "yes" else (1.0 - yes_price)
            unreal = r["contracts"] * (cur_val - r["fill_price"])
            self._c.execute(
                "UPDATE trades SET last_mark_price=?,last_mark_ts=?,unrealized_pnl=? WHERE id=?",
                (yes_price, self.clock(), unreal, r["id"]))
        self._c.commit()

    @_synchronized
    def resolve(self, market_id: str, outcome: str) -> int:
        n = 0
        for r in self._c.execute(
                "SELECT id,side,contracts,fill_price,fee FROM trades WHERE market_id=? AND status='placed'",
                (market_id,)).fetchall():
            won = (outcome == r["side"])
            payout = 1.0 if won else 0.0
            realized = r["contracts"] * (payout - r["fill_price"]) - (r["fee"] or 0.0)
            self._c.execute(
                """UPDATE trades SET status='resolved',outcome=?,realized_pnl=?,council_correct=?,
                   resolved_ts=? WHERE id=?""",
                (outcome, realized, 1 if won else 0, self.clock(), r["id"]))
            n += 1
        # Predictions on skipped debates resolve too (calibration data, no P&L).
        self._c.execute(
            """UPDATE trades SET status='resolved_skip', outcome=?, resolved_ts=?,
               council_correct=CASE WHEN (converged_p>=0.5)=(?='yes') THEN 1 ELSE 0 END
               WHERE market_id=? AND status='skipped'""",
            (outcome, self.clock(), outcome, market_id))
        # Resting orders that never filled just die with the market.
        self._c.execute("UPDATE trades SET status='cancelled', resolved_ts=? "
                        "WHERE market_id=? AND status='working'",
                        (self.clock(), market_id))
        self._c.commit()
        return n

    @_synchronized
    def open_positions(self) -> list:
        """Rows for every still-open real position (status='placed'). The exit loop
        prices each against a live quote each tick."""
        return self._c.execute(
            "SELECT id,market_id,side,fill_price,converged_p,contracts,fee "
            "FROM trades WHERE status='placed'").fetchall()

    @_synchronized
    def record_exit(self, trade_id: int, exit_price: float, exit_fee: float,
                    fill_count: int) -> float | None:
        """Close a position early. Realized P&L is booked in the side's own price terms
        (same convention as mark/resolve): contracts*(exit - entry) - entry_fee - exit_fee."""
        r = self._c.execute(
            "SELECT fill_price,fee FROM trades WHERE id=? AND status='placed'", (trade_id,)).fetchone()
        if r is None:
            return None
        realized = fill_count * (exit_price - r["fill_price"]) - (r["fee"] or 0.0) - exit_fee
        self._c.execute(
            "UPDATE trades SET status='exited',realized_pnl=?,last_mark_price=?,resolved_ts=? "
            "WHERE id=?", (realized, exit_price, self.clock(), trade_id))
        self._c.commit()
        return realized

    @_synchronized
    def mark_filled(self, trade_id: int, fill_price: float, fee: float, fill_count: int) -> None:
        """A resting maker order filled: promote 'working' -> 'placed' with real fill data."""
        self._c.execute(
            "UPDATE trades SET status='placed', fill_price=?, fee=?, fill_count=?, contracts=? "
            "WHERE id=? AND status='working'",
            (fill_price, fee, fill_count, fill_count, trade_id))
        self._c.commit()

    @_synchronized
    def cancel(self, trade_id: int) -> None:
        self._c.execute("UPDATE trades SET status='cancelled', resolved_ts=? "
                        "WHERE id=? AND status='working'", (self.clock(), trade_id))
        self._c.commit()

    @_synchronized
    def calibration_report(self, min_n: int = 50) -> dict:
        """Realized Brier of the council vs the market-price baseline over ALL resolved
        predictions (traded or skipped). The honesty gate for live arming."""
        rows = self._c.execute(
            """SELECT converged_p, blind_p, market_price, outcome FROM trades
               WHERE status IN ('resolved','resolved_skip') AND outcome IN ('yes','no')
               AND converged_p IS NOT NULL AND market_price IS NOT NULL""").fetchall()
        if not rows:
            return {"n": 0, "brier_model": None, "brier_market": None,
                    "brier_blind": None, "beats_market": False}
        outcomes = [1 if r["outcome"] == "yes" else 0 for r in rows]
        b_model = sum(brier(r["converged_p"], o) for r, o in zip(rows, outcomes)) / len(rows)
        b_market = sum(brier(r["market_price"], o) for r, o in zip(rows, outcomes)) / len(rows)
        blind_rows = [(r, o) for r, o in zip(rows, outcomes) if r["blind_p"] is not None]
        b_blind = (sum(brier(r["blind_p"], o) for r, o in blind_rows) / len(blind_rows)
                   if blind_rows else None)
        return {"n": len(rows), "brier_model": round(b_model, 6),
                "brier_market": round(b_market, 6),
                "brier_blind": round(b_blind, 6) if b_blind is not None else None,
                "beats_market": len(rows) >= min_n and b_model < b_market}

    @_synchronized
    def calibration(self) -> dict:
        rows = self._c.execute(
            "SELECT edge,council_correct FROM trades WHERE status='resolved'").fetchall()
        out = {"overall": {"n": 0, "win": 0},
               "by_edge": {b[0]: {"n": 0, "win": 0} for b in self._EDGE_BUCKETS}}
        for r in rows:
            out["overall"]["n"] += 1
            out["overall"]["win"] += r["council_correct"] or 0
            e = abs(r["edge"] or 0.0)
            for name, lo, hi in self._EDGE_BUCKETS:
                if lo <= e < hi:
                    out["by_edge"][name]["n"] += 1
                    out["by_edge"][name]["win"] += r["council_correct"] or 0
                    break
        return out

    @_synchronized
    def recall(self, market_id: str) -> str:
        series = _series(market_id)
        sim = self._c.execute(
            """SELECT council_correct FROM trades WHERE series=? AND status='resolved'
               ORDER BY resolved_ts DESC LIMIT 5""", (series,)).fetchall()
        cal = self.calibration()
        if cal["overall"]["n"] == 0:
            return "LESSONS: no resolved history yet."
        lines = ["LESSONS (your own track record):"]
        if sim:
            w = sum(r["council_correct"] or 0 for r in sim)
            lines.append(f"- {series}: {w}W/{len(sim) - w}L over your last {len(sim)} resolved trades here.")
        o = cal["overall"]
        lines.append(f"- Overall: {o['win']}/{o['n']} correct ({round(100 * o['win'] / o['n'])}%).")
        big = cal["by_edge"][">25c"]
        if big["n"]:
            lines.append(f"- Your >25c 'edges': {big['win']}/{big['n']} correct — treat large "
                         f"disagreements with the market as likely misreads.")
        return "\n".join(lines)

    @_synchronized
    def realized_today(self) -> float:
        """Sum of realized P&L for trades that settled since local midnight — feeds the
        daily-loss kill cap (which can't see real losses otherwise)."""
        start = datetime.datetime.fromtimestamp(self.clock()).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        r = self._c.execute(
            "SELECT COALESCE(SUM(realized_pnl),0.0) AS p FROM trades "
            "WHERE status IN ('resolved','exited') AND resolved_ts>=?", (start,)).fetchone()
        return float(r["p"] or 0.0)
