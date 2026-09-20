# The Learning Loop / "The Brain" (Phase B) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the council a memory — a trade journal that logs every decision, marks it to market, backfills the true outcome on resolution, and feeds that record back into future debates so it stops repeating mistakes.

**Architecture:** A self-contained `journal.py` (SQLite, like `artifacts.py`) with a pure, clock-injected API. The floor logs decisions, recalls lessons before each debate, and runs the marker/resolver on tick cadences. A best-effort vault mirror writes browsable notes. Additive: when no journal DB / vault is configured it no-ops, so existing behavior is unchanged.

**Tech Stack:** Python 3.14, stdlib `sqlite3`, pytest with in-memory DB + injected clock (no keys/network).

## Global Constraints
- All tests run offline, no API keys; journal uses `:memory:` SQLite and an injected `clock`.
- Journal must be **additive** — `FloorState()` with the default in-memory journal must not break any of the existing 80 tests.
- P&L convention: `fill_price` is stored in the **side's own terms** (the price paid for that YES/NO contract, 0–1). `realized_pnl = contracts * (payout - fill_price) - fee`, where `payout = 1.0` if the taken side won else `0.0`.
- `council_correct` = the taken side won (`outcome == side`).
- `series` = the ticker family: `market_id.split("-")[0]` (e.g. `KXHORMUZWEEKLY`).
- Calibration edge buckets: `<5c`, `5-15c`, `15-25c`, `>25c` (on the placed `edge`, in cents).

---

### Task 1: Journal store — schema, log, mark, resolve

**Files:**
- Create: `src/council/trading/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Produces:
  - `class Journal(path=":memory:", clock=time.time)`
  - `log(*, market_id, title, side, converged_p, spread, market_price, executable_price, edge, contracts, fill_price, fee, fill_count, decision_reason, rationale, status="placed") -> int`
  - `mark(market_id, yes_price) -> None` — updates open rows' `last_mark_*` + `unrealized_pnl`
  - `resolve(market_id, outcome) -> int` — settles open rows; sets `realized_pnl`, `council_correct`, `status="resolved"`
  - `_series(market_id) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_journal.py
from council.trading.journal import Journal

def _j():
    return Journal(":memory:", clock=lambda: 1000.0)

def _log(j, **kw):
    base = dict(market_id="KXHORMUZWEEKLY-26JUN21-T50", title="t", side="no",
                converged_p=0.34, spread=0.02, market_price=0.93, executable_price=0.07,
                edge=0.10, contracts=3, fill_price=0.07, fee=0.01, fill_count=3,
                decision_reason="r", rationale="debate")
    base.update(kw); return j.log(**base)

def test_log_creates_open_row():
    j=_j(); tid=_log(j)
    row=j.get(tid)
    assert row["status"]=="placed" and row["series"]=="KXHORMUZWEEKLY" and row["side"]=="no"
    assert row["outcome"] is None and row["realized_pnl"] is None

def test_mark_updates_unrealized_no_side():
    j=_j(); tid=_log(j, side="no", fill_price=0.07, contracts=3)
    j.mark("KXHORMUZWEEKLY-26JUN21-T50", yes_price=0.80)   # NO now worth 0.20
    row=j.get(tid)
    assert abs(row["unrealized_pnl"] - 3*(0.20-0.07)) < 1e-9   # +0.39

def test_resolve_no_win_and_yes_win():
    j=_j()
    no_t=_log(j, side="no", contracts=3, fill_price=0.07, fee=0.01)
    j.resolve("KXHORMUZWEEKLY-26JUN21-T50", outcome="no")     # NO won
    r=j.get(no_t)
    assert r["status"]=="resolved" and r["council_correct"]==1
    assert abs(r["realized_pnl"] - (3*(1.0-0.07)-0.01)) < 1e-9   # +2.78

    yes_t=_log(j, market_id="KXFOO-1", side="yes", contracts=2, fill_price=0.60, fee=0.01)
    j.resolve("KXFOO-1", outcome="no")                        # YES lost
    r2=j.get(yes_t)
    assert r2["council_correct"]==0
    assert abs(r2["realized_pnl"] - (2*(0.0-0.60)-0.01)) < 1e-9  # -1.21
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_journal.py -v`
Expected: FAIL (`ModuleNotFoundError: council.trading.journal`)

- [ ] **Step 3: Implement** `src/council/trading/journal.py`

```python
"""Trade journal — the council's memory. SQLite store of every decision, its marks, and its
true outcome once the market resolves. Pure + clock-injected for deterministic tests."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable


def _series(market_id: str) -> str:
    return market_id.split("-")[0]


class Journal:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._c = sqlite3.connect(path)
        self._c.row_factory = sqlite3.Row
        self._c.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, opened_ts REAL, market_id TEXT, series TEXT,
            title TEXT, side TEXT, converged_p REAL, spread REAL, market_price REAL,
            executable_price REAL, edge REAL, contracts INTEGER, fill_price REAL, fee REAL,
            fill_count INTEGER, decision_reason TEXT, rationale TEXT, status TEXT,
            last_mark_price REAL, last_mark_ts REAL, unrealized_pnl REAL,
            outcome TEXT, realized_pnl REAL, council_correct INTEGER, resolved_ts REAL)""")
        self._c.commit()

    def log(self, *, market_id, title, side, converged_p, spread, market_price,
            executable_price, edge, contracts, fill_price, fee, fill_count,
            decision_reason, rationale, status="placed") -> int:
        cur = self._c.execute(
            """INSERT INTO trades(opened_ts,market_id,series,title,side,converged_p,spread,
               market_price,executable_price,edge,contracts,fill_price,fee,fill_count,
               decision_reason,rationale,status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.clock(), market_id, _series(market_id), title, side, converged_p, spread,
             market_price, executable_price, edge, contracts, fill_price, fee, fill_count,
             decision_reason, rationale, status))
        self._c.commit()
        return cur.lastrowid

    def get(self, trade_id: int) -> dict:
        r = self._c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(r) if r else None

    def mark(self, market_id: str, yes_price: float) -> None:
        for r in self._c.execute(
                "SELECT id,side,fill_price,contracts FROM trades WHERE market_id=? AND status='placed'",
                (market_id,)).fetchall():
            cur_val = yes_price if r["side"] == "yes" else (1.0 - yes_price)
            unreal = r["contracts"] * (cur_val - r["fill_price"])
            self._c.execute("UPDATE trades SET last_mark_price=?,last_mark_ts=?,unrealized_pnl=? WHERE id=?",
                            (yes_price, self.clock(), unreal, r["id"]))
        self._c.commit()

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
        self._c.commit()
        return n
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/bin/pytest tests/test_journal.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/journal.py tests/test_journal.py
git commit -m "feat(journal): SQLite trade journal — log, mark-to-market, resolve"
```

---

### Task 2: Recall + calibration

**Files:**
- Modify: `src/council/trading/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Produces: `calibration() -> dict`, `recall(market_id) -> str`

- [ ] **Step 1: Write the failing test** (append to `tests/test_journal.py`)

```python
def test_calibration_buckets():
    j=_j()
    # 2 small-edge resolved (1 win), 1 big-edge resolved (loss)
    a=_log(j, market_id="KXA-1", edge=0.03, side="yes", fill_price=0.5, contracts=1, fee=0)
    j.resolve("KXA-1","yes")
    b=_log(j, market_id="KXB-1", edge=0.04, side="yes", fill_price=0.5, contracts=1, fee=0)
    j.resolve("KXB-1","no")
    c=_log(j, market_id="KXC-1", edge=0.30, side="no", fill_price=0.1, contracts=1, fee=0)
    j.resolve("KXC-1","yes")
    cal=j.calibration()
    assert cal["overall"]["n"]==3 and cal["overall"]["win"]==1
    assert cal["by_edge"]["<5c"]["n"]==2 and cal["by_edge"]["<5c"]["win"]==1
    assert cal["by_edge"][">25c"]["n"]==1 and cal["by_edge"][">25c"]["win"]==0

def test_recall_similar_and_empty():
    j=_j()
    assert "no resolved history" in j.recall("KXHORMUZWEEKLY-26JUN21-T50").lower()
    t=_log(j, market_id="KXHORMUZWEEKLY-26JUN21-T30", side="no", contracts=1, fill_price=0.9, fee=0)
    j.resolve("KXHORMUZWEEKLY-26JUN21-T30","yes")   # NO lost
    text=j.recall("KXHORMUZWEEKLY-26JUN21-T99")
    assert "KXHORMUZWEEKLY" in text and ("0W/1L" in text or "1L" in text)
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_journal.py -k "calibration or recall" -v`
Expected: FAIL (`AttributeError: 'Journal' object has no attribute 'calibration'`)

- [ ] **Step 3: Implement** (append methods to `Journal`)

```python
    _EDGE_BUCKETS = [("<5c", 0.0, 0.05), ("5-15c", 0.05, 0.15), ("15-25c", 0.15, 0.25), (">25c", 0.25, 9.0)]

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

    def recall(self, market_id: str) -> str:
        series = _series(market_id)
        sim = self._c.execute(
            """SELECT side,council_correct FROM trades WHERE series=? AND status='resolved'
               ORDER BY resolved_ts DESC LIMIT 5""", (series,)).fetchall()
        cal = self.calibration()
        if cal["overall"]["n"] == 0:
            return "LESSONS: no resolved history yet."
        lines = ["LESSONS (your own track record):"]
        if sim:
            w = sum(r["council_correct"] or 0 for r in sim)
            lines.append(f"- {series}: {w}W/{len(sim)-w}L over your last {len(sim)} resolved trades here.")
        o = cal["overall"]
        lines.append(f"- Overall: {o['win']}/{o['n']} correct ({round(100*o['win']/o['n'])}%).")
        big = cal["by_edge"][">25c"]
        if big["n"]:
            lines.append(f"- Your >25c 'edges': {big['win']}/{big['n']} correct — treat large "
                         f"disagreements with the market as likely misreads.")
        return "\n".join(lines)
```

- [ ] **Step 4: Run, expect pass + full suite**

Run: `.venv/bin/pytest tests/test_journal.py -q && .venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/journal.py tests/test_journal.py
git commit -m "feat(journal): recall (similar trades) + calibration stats"
```

---

### Task 3: Floor logging + lessons into the debate

**Files:**
- Modify: `src/council/trading/deliberation.py` (`debate(..., lessons="")`, `_market_block`)
- Modify: `src/council/trading/floor.py` (construct journal; recall before debate; log on decision)
- Test: `tests/test_deliberation.py`, `tests/test_floor_council.py`

**Interfaces:**
- Consumes: `Journal.recall`, `Journal.log`.
- Produces: `DeliberativeCouncil.debate(market, notes, lessons="")`; `FloorState.journal`.

- [ ] **Step 1: Write failing tests**

In `tests/test_deliberation.py`:
```python
def test_debate_prompt_includes_lessons():
    client = RoundtableClient({s.model: 0.5 for s in _PANEL})
    m = Market("X", "x?", 0.5)
    DeliberativeCouncil(_PANEL, client).debate(m, "notes", lessons="LESSONS: your >25c edges 1/8.")
    allmsgs = " ".join(msg["content"] for conv in client.messages for msg in conv)
    assert "your >25c edges 1/8" in allmsgs
```
In `tests/test_floor_council.py`:
```python
def test_floor_logs_trade_on_fill():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f._council_eval()
    rows = f.journal._c.execute("SELECT side,status,fill_count FROM trades").fetchall()
    assert any(r["status"]=="placed" and r["side"]=="no" for r in rows)
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_deliberation.py::test_debate_prompt_includes_lessons tests/test_floor_council.py::test_floor_logs_trade_on_fill -v`
Expected: FAIL (no `lessons` param / no `journal`)

- [ ] **Step 3: Implement**

In `deliberation.py`, change `debate` signature and `_market_block`:
```python
    def debate(self, market: Market, notes: str, lessons: str = "") -> Deliberation:
```
Pass `lessons` into the prefix: change the `prefix = _market_block(market, notes)` line to
`prefix = _market_block(market, notes, lessons)`, and update `_market_block`:
```python
def _market_block(market: Market, notes: str, lessons: str = "") -> str:
    quote = (f"Live quote: YES bid {market.yes_bid:.2f} / ask {market.yes_ask:.2f}\n"
             if (market.yes_ask or market.yes_bid) else "")
    rules = f"Resolution rules: {market.rules}\n" if market.rules else ""
    les = f"{lessons}\n" if lessons else ""
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"Current YES price: {market.yes_price:.2f}\n"
            f"{quote}{rules}{les}Research notes:\n{notes}\n")
```

In `floor.py`:
- import: `from .journal import Journal`
- in `__init__`: `self.journal = Journal(os.environ.get("JOURNAL_PATH", ":memory:"))`
- in `_council_eval`, replace the debate line and add logging. Find:
```python
        notes = self.research.context_for(m) if self.research else "No external signal available."
        d = self.council.debate(m, notes)
        dec = decide(d, m, self.guard, edge_threshold=self.EDGE_THRESHOLD, spread_cap=self.SPREAD_CAP)
        self.last_debate = (d, dec)
```
  replace with:
```python
        notes = self.research.context_for(m) if self.research else "No external signal available."
        lessons = self.journal.recall(m.id) if self.journal else ""
        d = self.council.debate(m, notes, lessons)
        dec = decide(d, m, self.guard, edge_threshold=self.EDGE_THRESHOLD, spread_cap=self.SPREAD_CAP)
        self.last_debate = (d, dec)
```
- add a helper and call it where trades happen. After the placement branches in `_council_eval`,
  log the journal entry. Simplest: have `_auto_execute` stash the fill, and log in `_council_eval`.
  In `_auto_execute`, right before `return True`, add: `self._last_fill = {"price": fill_px, "count": filled}`; and at the top set nothing. In `_council_eval`, after the placement block, add:
```python
        rationale = " | ".join(f"{t.model} {t.p_yes:.2f}" for t in d.round2)
        if not dec.place:
            self.journal.log(market_id=m.id, title=m.title, side=(dec.side or "n/a"),
                converged_p=d.converged_p, spread=d.spread, market_price=m.yes_price,
                executable_price=dec.limit_price_cents/100.0, edge=0.0, contracts=0,
                fill_price=0.0, fee=0.0, fill_count=0, decision_reason=dec.reason,
                rationale=rationale, status="skipped")
            return
        # ... existing place/paper/ticket branches run; for armed fills log placed:
```
  After `if self._auto_execute(...)` succeeds (armed branch), log:
```python
            fill = getattr(self, "_last_fill", None) or {"price": entry, "count": dec.contracts}
            self.journal.log(market_id=m.id, title=m.title, side=dec.side,
                converged_p=d.converged_p, spread=d.spread, market_price=m.yes_price,
                executable_price=fill["price"], edge=abs(d.converged_p - m.yes_price),
                contracts=fill["count"], fill_price=fill["price"],
                fee=kalshi_fee(fill["price"], fill["count"]), fill_count=fill["count"],
                decision_reason=dec.reason, rationale=rationale, status="placed")
```
  (Place the SKIP log before the `if not dec.place: return`, and the placed log inside the armed
  success branch. Mirror similarly for the paper-fill branch with status="placed".)

- [ ] **Step 4: Run full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (existing 80 + new; in-memory journal is additive). If an existing floor test
breaks, STOP and report — do not weaken it.

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/deliberation.py src/council/trading/floor.py tests/
git commit -m "feat(council): log every decision + feed recalled lessons into the debate"
```

---

### Task 4: Marker + resolver on the tick loop

**Files:**
- Modify: `src/council/trading/floor.py`
- Test: `tests/test_floor_council.py`

**Interfaces:**
- Produces: `FloorState.mark_open()`, `FloorState.resolve_settled()`, called from `tick()`.

- [ ] **Step 1: Write the failing test**

```python
def test_resolver_settles_open_trades():
    from council.trading.market import Market
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f._council_eval()                      # logs an open NO trade on FED-DEC-CUT
    # fake market data that reports the market resolved YES
    class _MD:
        def get_market(self, tk): return Market(tk, "t", 1.0, status="resolved", outcome=1)
    f._market_data = _MD()
    f.resolve_settled()
    row = f.journal._c.execute("SELECT status,outcome FROM trades WHERE status='resolved'").fetchone()
    assert row is not None and row["outcome"]=="yes"
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_floor_council.py::test_resolver_settles_open_trades -v`
Expected: FAIL (`no attribute 'resolve_settled'`)

- [ ] **Step 3: Implement** in `floor.py`

Add cadence knobs in `__init__`: `self.MARK_EVERY=int(os.environ.get("MARK_EVERY",20)); self.RESOLVE_EVERY=int(os.environ.get("RESOLVE_EVERY",40)); self._market_data=None`.
Add a lazy market-data accessor + the two methods:
```python
    def _md(self):
        if self._market_data is None:
            from .market import KalshiMarketData
            kid, pk = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
            self._market_data = KalshiMarketData(kid, pk) if kid and pk else None
        return self._market_data

    def _open_market_ids(self):
        return [r["market_id"] for r in self.journal._c.execute(
            "SELECT DISTINCT market_id FROM trades WHERE status='placed'").fetchall()]

    def mark_open(self) -> None:
        md = self._md()
        if not md: return
        for mid in self._open_market_ids():
            try:
                m = md.get_market(mid)
                if m: self.journal.mark(mid, m.yes_price)
            except Exception: pass   # noqa: BLE001

    def resolve_settled(self) -> None:
        md = self._md()
        if not md: return
        for mid in self._open_market_ids():
            try:
                m = md.get_market(mid)
                if m and m.status == "resolved" and m.outcome is not None:
                    self.journal.resolve(mid, "yes" if m.outcome == 1 else "no")
            except Exception: pass   # noqa: BLE001
```
In `tick()`, after the existing resolve/eval logic, add:
```python
        if self._tick_n % self.MARK_EVERY == 0:
            self.mark_open()
        if self._tick_n % self.RESOLVE_EVERY == 0:
            self.resolve_settled()
```
(The test sets `f._market_data` directly so `_md()` returns the fake.)

- [ ] **Step 4: Run, expect pass + full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/floor.py tests/test_floor_council.py
git commit -m "feat(floor): mark-to-market + resolver backfill on the tick loop"
```

---

### Task 5: Vault mirror

**Files:**
- Create: `src/council/trading/journal_mirror.py`
- Modify: `src/council/trading/journal.py` (optional hook) and `src/council/trading/floor.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Produces: `mirror_trade(vault_dir, row: dict) -> None` — writes/updates `vault_dir/wiki/trades/<market_id>.md`; best-effort.

- [ ] **Step 1: Write the failing test**

```python
def test_mirror_writes_note(tmp_path):
    from council.trading.journal_mirror import mirror_trade
    row = {"market_id":"KXFOO-1","title":"Foo?","side":"no","contracts":3,"fill_price":0.07,
           "status":"placed","decision_reason":"r","rationale":"Claude 0.34 | Kimi 0.33",
           "outcome":None,"realized_pnl":None,"council_correct":None}
    mirror_trade(str(tmp_path), row)
    note = tmp_path/"wiki"/"trades"/"KXFOO-1.md"
    assert note.exists() and "Foo?" in note.read_text() and "NO" in note.read_text().upper()

def test_mirror_failure_is_silent():
    from council.trading.journal_mirror import mirror_trade
    mirror_trade("/nonexistent/\0bad", {"market_id":"X"})  # must not raise
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_journal.py -k mirror -v`
Expected: FAIL (`ModuleNotFoundError: journal_mirror`)

- [ ] **Step 3: Implement** `src/council/trading/journal_mirror.py`

```python
"""Best-effort mirror of journal trades into the Obsidian vault (browsable record).
Never raises into the trading loop."""
from __future__ import annotations

import pathlib


def mirror_trade(vault_dir: str, row: dict) -> None:
    try:
        d = pathlib.Path(vault_dir) / "wiki" / "trades"
        d.mkdir(parents=True, exist_ok=True)
        mid = row.get("market_id", "unknown")
        outcome = row.get("outcome")
        status = "RESOLVED" if outcome else row.get("status", "open").upper()
        pnl = row.get("realized_pnl")
        body = (f"---\ntype: trade\nmarket: \"{mid}\"\nstatus: {status.lower()}\n"
                f"side: {row.get('side')}\n---\n\n"
                f"# {row.get('title','')} ({mid})\n\n"
                f"- Side: **{str(row.get('side','')).upper()}** x{row.get('contracts')} "
                f"@ {row.get('fill_price')}\n"
                f"- Status: {status}\n"
                f"- Decision: {row.get('decision_reason','')}\n"
                f"- Council: {row.get('rationale','')}\n")
        if outcome:
            body += (f"- Outcome: **{str(outcome).upper()}** "
                     f"({'correct' if row.get('council_correct') else 'wrong'}), "
                     f"realized {pnl}\n")
        (d / f"{mid}.md").write_text(body)
    except Exception:  # noqa: BLE001 — mirroring must never break trading
        pass
```

In `floor.py` `__init__`: `self.vault_dir = (self.settings.obsidian.vault if getattr(self,'settings',None) else None)` — OR read the same config the project uses (confirm the key in `config.py`; if the floor has no settings handle, read `os.environ.get("COUNCIL_VAULT")` and document it). After each `self.journal.log(...)` and in `resolve_settled` after a resolve, call:
```python
        if self.vault_dir:
            from .journal_mirror import mirror_trade
            mirror_trade(self.vault_dir, self.journal.get(tid))
```
(Confirm the vault config key against `config.py`/`obsidian.py` during implementation; if absent, gate on `COUNCIL_VAULT` env and note it.)

- [ ] **Step 4: Run, expect pass + full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/journal_mirror.py src/council/trading/floor.py tests/test_journal.py
git commit -m "feat(journal): best-effort vault mirror of trades into the brain"
```

---

## Self-Review

**Spec coverage:** journal store §1→Task 1; recall+calibration §5→Task 2; logging hook §2 + recall injection §6→Task 3; marker §3 + resolver §4→Task 4; vault mirror §7→Task 5. All covered.

**Placeholders:** none — full code per step. Two implementation-time confirmations are flagged
explicitly (the floor's `_last_fill` stash in Task 3; the vault-config key in Task 5) — these are
"verify against existing code", not unfinished steps.

**Type consistency:** `Journal.log` kwargs (Task 1) match the floor's call (Task 3). `mark`/`resolve`/
`recall`/`calibration` signatures consistent across Tasks 1–4. `debate(market, notes, lessons="")`
(Task 3) is the only debate-signature change; callers updated in the same task. `Market.status`/
`outcome` used by the resolver (Task 4) exist on `Market`.

**Additive/backward-compat:** journal defaults to `:memory:`; logging/mark/resolve don't touch the
real-money path; existing 80 tests must stay green (guarded at Steps 4 of Tasks 3–5).

## Out of scope
Auto-tuning the gate from calibration; position management/closing; semantic (embedding) similarity.
