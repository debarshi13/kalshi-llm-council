# Exit Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the bot the ability to *close* a position early (take-profit or edge-decay) instead of only entering and holding to settlement.

**Architecture:** A pure decision core (`exits.py`) decides exit/hold from a position + a live quote, with zero I/O. The journal gains `open_positions()` + `record_exit()`. `KalshiTrader` gains an explicit `action="sell"` close path (verified on the demo host before live). `floor.py` runs an `_exit_eval()` pass each tick that prices each open position off a fresh Kalshi quote and places the close order. No LLM calls in the exit loop.

**Tech Stack:** Python 3.14, pytest, SQLite (`sqlite3`), `litellm` (live only), `httpx` (Kalshi REST), `cryptography` (request signing).

## Global Constraints

- **Money is in dollars (0.0–1.0), not cents**, everywhere except `KalshiTrader.build_order`/`place_order` which take a `limit_price_cents` int in 1–99. Match the existing convention; do not introduce cents elsewhere.
- **Open position = `trades` row with `status='placed'`.** `mark()` and `resolve()` already query `status='placed'`; exits introduce `status='exited'`.
- **Exits must bypass the risk guard.** Closing reduces exposure — never call `guard.check()` on the exit path.
- **No LLM calls in the exit loop.** Pricing uses Kalshi REST quotes only (the same `self._md().get_market()` the existing `mark_open()` uses).
- **`build_order`/`place_order` must stay backward compatible** — add `action="buy"` as a defaulted last parameter so every existing call site is unchanged.
- **Real money.** The sell wire-format is the only piece not already verified in this repo; Task 5 (demo-host round trip) is a hard gate before any live exit.
- Run the full suite with `python -m pytest -q` from `~/council`. Tests must not need network or API keys.

---

### Task 1: Pure exit-decision core (`exits.py`)

**Files:**
- Create: `src/council/trading/exits.py`
- Test: `tests/test_exits.py`

**Interfaces:**
- Consumes: `Market` from `council.trading.market` (reads `m.yes_bid`, `m.yes_ask`, dollars).
- Produces:
  - `OpenPosition(trade_id:int, market_id:str, side:str, entry_price:float, fair_value:float, contracts:int, entry_fee:float)` — frozen dataclass.
  - `ExitParams(take_profit:float=0.05, exit_edge:float=0.01, stop_loss:float|None=None)` — frozen dataclass.
  - `ExitDecision(should_exit:bool, reason:str="", exit_price:float=0.0)` — frozen dataclass.
  - `exit_signal(pos:OpenPosition, m:Market, params:ExitParams) -> ExitDecision`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_exits.py
import pytest
from council.trading.market import Market
from council.trading.exits import OpenPosition, ExitParams, ExitDecision, exit_signal

def _pos(side="yes", entry=0.40, fair=0.55, contracts=10):
    return OpenPosition(trade_id=1, market_id="X-1", side=side, entry_price=entry,
                        fair_value=fair, contracts=contracts, entry_fee=0.01)

def _mkt(yes_bid=0.40, yes_ask=0.42):
    return Market("X-1", "t", yes_price=(yes_bid + yes_ask) / 2, yes_bid=yes_bid, yes_ask=yes_ask)

P = ExitParams(take_profit=0.05, exit_edge=0.01, stop_loss=None)

def test_hold_when_neither_trigger_fires():
    # bought yes @0.40, fair 0.55, market still ~0.42 -> gain 0.02 (<0.05), edge 0.13 (>0.01)
    d = exit_signal(_pos(), _mkt(yes_bid=0.42, yes_ask=0.44), P)
    assert d.should_exit is False

def test_take_profit_fires_on_favorable_move():
    # yes bid rose to 0.46 -> gain 0.06 >= 0.05
    d = exit_signal(_pos(entry=0.40), _mkt(yes_bid=0.46, yes_ask=0.48), P)
    assert d.should_exit is True and "take-profit" in d.reason and d.exit_price == 0.46

def test_edge_decay_fires_when_market_reaches_fair_value():
    # fair 0.55, yes bid 0.55 -> remaining edge 0.00 <= 0.01. Entry high so TP does NOT fire first.
    d = exit_signal(_pos(entry=0.52, fair=0.55), _mkt(yes_bid=0.55, yes_ask=0.57), P)
    assert d.should_exit is True and "edge" in d.reason

def test_no_side_take_profit_uses_no_bid():
    # short yes / long no: entry no-price 0.40 (yes 0.60), fair_value(yes)=0.45 -> fair(no)=0.55.
    # yes_ask 0.54 -> no_bid = 1-0.54 = 0.46 -> gain 0.06 >= 0.05.
    d = exit_signal(_pos(side="no", entry=0.40, fair=0.45), _mkt(yes_bid=0.52, yes_ask=0.54), P)
    assert d.should_exit is True and "take-profit" in d.reason and d.exit_price == 0.46

def test_stop_loss_off_by_default_holds_a_loser():
    # bought yes @0.40, bid dropped to 0.30 -> down 0.10, but stop_loss None and edge grew -> HOLD
    d = exit_signal(_pos(entry=0.40, fair=0.55), _mkt(yes_bid=0.30, yes_ask=0.32), P)
    assert d.should_exit is False

def test_stop_loss_fires_when_enabled():
    params = ExitParams(take_profit=0.05, exit_edge=0.01, stop_loss=0.08)
    d = exit_signal(_pos(entry=0.40, fair=0.55), _mkt(yes_bid=0.30, yes_ask=0.32), params)
    assert d.should_exit is True and "stop-loss" in d.reason

def test_missing_quote_holds():
    d = exit_signal(_pos(), _mkt(yes_bid=0.0, yes_ask=0.0), P)
    assert d.should_exit is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_exits.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'council.trading.exits'`.

- [ ] **Step 3: Write the implementation**

```python
# src/council/trading/exits.py
"""Pure exit-decision core — decides close/hold for an open position given a live quote.

No I/O, no LLM calls. Mirrors the scout/decide split: a deterministic function the floor
calls each tick. Money is in dollars (0.0-1.0), matching the rest of the trading package.
"""
from __future__ import annotations

from dataclasses import dataclass

from .market import Market


@dataclass(frozen=True)
class OpenPosition:
    trade_id: int
    market_id: str
    side: str            # "yes" | "no"
    entry_price: float   # the side's own fill price, dollars
    fair_value: float    # council converged probability, 0..1 (YES terms)
    contracts: int
    entry_fee: float


@dataclass(frozen=True)
class ExitParams:
    take_profit: float = 0.05      # close when up this many dollars vs entry
    exit_edge: float = 0.01        # close when remaining edge to fair value <= this
    stop_loss: float | None = None # if set, close when down this many dollars vs entry


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str = ""
    exit_price: float = 0.0        # the side's own price we'd sell into, dollars


def exit_signal(pos: OpenPosition, m: Market, params: ExitParams) -> ExitDecision:
    """Decide whether to close `pos` given live quote `m`. Triggers are OR'd.

    YES position: sell into the YES bid; fair value is the council prob.
    NO  position: sell into the NO bid (= 1 - yes_ask); fair value is (1 - prob).
    """
    if pos.side == "yes":
        bid = m.yes_bid
        fair = pos.fair_value
    else:  # "no" — short YES / long NO
        bid = round(1.0 - m.yes_ask, 4) if m.yes_ask else 0.0
        fair = round(1.0 - pos.fair_value, 4)

    if bid <= 0.0:                                   # no quote to price against
        return ExitDecision(False, "", 0.0)

    gain = round(bid - pos.entry_price, 4)
    remaining_edge = round(fair - bid, 4)

    if gain >= params.take_profit:
        return ExitDecision(True, f"take-profit +${gain:.2f}", bid)
    if remaining_edge <= params.exit_edge:
        return ExitDecision(True, f"edge decayed (${remaining_edge:.2f} <= ${params.exit_edge:.2f})", bid)
    if params.stop_loss is not None and (pos.entry_price - bid) >= params.stop_loss:
        return ExitDecision(True, f"stop-loss -${pos.entry_price - bid:.2f}", bid)
    return ExitDecision(False, "", bid)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_exits.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/exits.py tests/test_exits.py
git commit -m "feat(exits): pure take-profit + edge-decay exit-decision core"
```

---

### Task 2: Journal position lifecycle (`open_positions`, `record_exit`)

**Files:**
- Modify: `src/council/trading/journal.py` (add two methods to the `Journal` class, after `resolve`, ~line 95)
- Test: `tests/test_journal.py` (append)

**Interfaces:**
- Consumes: existing `Journal.log(..., status="placed")` to create open rows; existing `_synchronized`, `self._c`, `self.clock`.
- Produces:
  - `Journal.open_positions() -> list[sqlite3.Row]` — rows with `id, market_id, side, fill_price, converged_p, contracts, fee` where `status='placed'`.
  - `Journal.record_exit(trade_id:int, exit_price:float, exit_fee:float, fill_count:int) -> float|None` — sets `status='exited'`, computes and stores `realized_pnl = fill_count*(exit_price - fill_price) - entry_fee - exit_fee`, stamps `resolved_ts`, returns the realized P&L (or `None` if the id is unknown).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_journal.py  (append)
def test_open_positions_lists_only_placed():
    j = Journal(":memory:", clock=lambda: 1000.0)
    j.log(market_id="A-1", title="t", side="yes", converged_p=0.6, spread=0.02,
          market_price=0.5, executable_price=0.52, edge=0.08, contracts=10,
          fill_price=0.52, fee=0.01, fill_count=10, decision_reason="r",
          rationale="x", status="placed")
    j.log(market_id="B-2", title="t", side="no", converged_p=0.3, spread=0.02,
          market_price=0.4, executable_price=0.41, edge=0.05, contracts=5,
          fill_price=0.59, fee=0.01, fill_count=5, decision_reason="r",
          rationale="x", status="skipped")
    rows = j.open_positions()
    assert [r["market_id"] for r in rows] == ["A-1"]
    assert rows[0]["fill_price"] == 0.52 and rows[0]["converged_p"] == 0.6

def test_record_exit_closes_row_and_realizes_pnl():
    j = Journal(":memory:", clock=lambda: 1000.0)
    tid = j.log(market_id="A-1", title="t", side="yes", converged_p=0.6, spread=0.02,
                market_price=0.5, executable_price=0.52, edge=0.08, contracts=10,
                fill_price=0.50, fee=0.02, fill_count=10, decision_reason="r",
                rationale="x", status="placed")
    realized = j.record_exit(tid, exit_price=0.58, exit_fee=0.02, fill_count=10)
    # 10 * (0.58 - 0.50) - 0.02 entry - 0.02 exit = 0.80 - 0.04 = 0.76
    assert round(realized, 4) == 0.76
    row = j.get(tid)
    assert row["status"] == "exited" and round(row["realized_pnl"], 4) == 0.76
    assert j.open_positions() == []   # no longer open

def test_record_exit_unknown_id_returns_none():
    j = Journal(":memory:", clock=lambda: 1000.0)
    assert j.record_exit(999, 0.5, 0.0, 1) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_journal.py -q -k "open_positions or record_exit"`
Expected: FAIL — `AttributeError: 'Journal' object has no attribute 'open_positions'`.

- [ ] **Step 3: Write the implementation**

Add these two methods to the `Journal` class (after `resolve`, before `calibration`):

```python
    @_synchronized
    def open_positions(self):
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
            "SELECT fill_price,fee FROM trades WHERE id=?", (trade_id,)).fetchone()
        if r is None:
            return None
        realized = fill_count * (exit_price - r["fill_price"]) - (r["fee"] or 0.0) - exit_fee
        self._c.execute(
            "UPDATE trades SET status='exited',realized_pnl=?,last_mark_price=?,resolved_ts=? "
            "WHERE id=?", (realized, exit_price, self.clock(), trade_id))
        self._c.commit()
        return realized
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_journal.py -q`
Expected: PASS (all journal tests, including the 3 new ones).

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/journal.py tests/test_journal.py
git commit -m "feat(journal): open_positions() + record_exit() for early closes"
```

---

### Task 3: `KalshiTrader` sell/close order body

**Files:**
- Modify: `src/council/trading/execution.py` (`build_order` ~line 73, `place_order` ~line 103)
- Test: `tests/test_execution.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `KalshiTrader.build_order(ticker, side, count, limit_price_cents, action="buy") -> dict` — when `action="sell"` the body carries `"action": "sell"`; buys are byte-for-byte unchanged.
  - `KalshiTrader.place_order(ticker, side, count, limit_price_cents, action="buy") -> dict` — forwards `action`.

Note: buys keep the existing verified `{"side": "bid"/"ask"}` body and add no `action` key (preserving the verified shape). Sells add `"action": "sell"`. The exact accepted sell shape is **verified live in Task 5** before any real exit runs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_execution.py  (append)
def test_build_order_buy_is_unchanged_no_action_key():
    b = KalshiTrader.build_order("X", "yes", 10, 62)        # default action="buy"
    assert b["side"] == "bid" and b["price"] == "0.6200"
    assert "action" not in b                                # buy body must stay byte-identical

def test_build_order_sell_yes_carries_action_sell():
    b = KalshiTrader.build_order("X", "yes", 10, 55, action="sell")
    assert b["action"] == "sell"
    assert b["side"] == "bid" and b["price"] == "0.5500"    # sell yes -> hit the yes bid
    assert b["time_in_force"] == "immediate_or_cancel"

def test_build_order_sell_no_carries_action_sell():
    b = KalshiTrader.build_order("X", "no", 5, 40, action="sell")  # sell no @40c
    assert b["action"] == "sell" and b["side"] == "ask" and b["price"] == "0.6000"

def test_build_order_rejects_bad_action():
    with pytest.raises(ValueError):
        KalshiTrader.build_order("X", "yes", 1, 50, action="hold")
```

(If `pytest` isn't already imported at the top of `tests/test_execution.py`, add `import pytest`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_execution.py -q -k "action or buy_is_unchanged"`
Expected: FAIL — `TypeError: build_order() got an unexpected keyword argument 'action'`.

- [ ] **Step 3: Write the implementation**

Replace the `build_order` signature/body and `place_order` signature. New `build_order`:

```python
    @staticmethod
    def build_order(ticker: str, side: str, count: int, limit_price_cents: int,
                    action: str = "buy") -> dict:
        """Construct a Kalshi V2 order body. Pure — safe to unit-test offline.

        action="buy"  -> open/add: buy YES @bid, buy NO @ask (the verified shape; no action key)
        action="sell" -> close: sell the side we hold, crossing into its bid; adds "action":"sell"
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be yes/no, got {side!r}")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be buy/sell, got {action!r}")
        if count < 1:
            raise ValueError("count must be >= 1")
        cents = int(round(limit_price_cents))
        if not 1 <= cents <= 99:
            raise ValueError(f"limit price must be 1-99 cents, got {cents}")
        if side == "yes":
            v2_side, price = "bid", cents / 100.0
        else:
            v2_side, price = "ask", (100 - cents) / 100.0
        body = {
            "ticker": ticker,
            "side": v2_side,
            "count": f"{int(count):.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": str(uuid.uuid4()),
        }
        if action == "sell":
            body["action"] = "sell"     # close an existing position; verified on demo (see plan Task 5)
        return body
```

New `place_order` signature (only the signature + the `build_order` call change):

```python
    def place_order(self, ticker: str, side: str, count: int, limit_price_cents: int,
                    action: str = "buy") -> dict:
        """LIVE — spends/realizes real money. Posts to the Kalshi V2 create-order endpoint."""
        import httpx

        path = self.PREFIX + self.ORDER_PATH
        body = self.build_order(ticker, side, count, limit_price_cents, action)
        resp = httpx.post(self.host + path, headers=self._signed_headers("POST", path), json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_execution.py -q`
Expected: PASS (existing buy tests still pass — the buy body is unchanged — plus the 4 new ones).

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/execution.py tests/test_execution.py
git commit -m "feat(execution): action=sell close path on build_order/place_order"
```

---

### Task 4: Wire `_exit_eval` into the floor

**Files:**
- Modify: `src/council/trading/floor.py` (`__init__` to set `self._exit_params`; add `_exit_eval`/`_place_exit`; call from `tick`)
- Test: `tests/test_floor_council.py` (append)

**Interfaces:**
- Consumes: `OpenPosition`, `exit_signal` (Task 1); `journal.open_positions()`, `journal.record_exit()` (Task 2); `place_order(..., action="sell")` (Task 3); existing `self._md()`, `self.journal`, `self.books`, `self._mirror`, `self._log`, `kalshi_fee`.
- Produces: `FloorState._exit_eval()` and `FloorState._place_exit(pos, m, sig)`; `self._exit_params: ExitParams`.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_floor_council.py  (append)
from council.trading.market import Market

class FakeMD:
    """Stands in for KalshiMarketData.get_market in exit pricing."""
    def __init__(self, market): self._m = market
    def get_market(self, mid): return self._m if self._m and self._m.id == mid else None

def _armed_floor_with_open_yes():
    f = FloorState()
    f.live = True
    f.execute = True
    # an open YES position: 10 contracts @0.50, council fair value 0.62
    f.journal.log(market_id="Z-1", title="t", side="yes", converged_p=0.62, spread=0.02,
                  market_price=0.50, executable_price=0.52, edge=0.10, contracts=10,
                  fill_price=0.50, fee=0.02, fill_count=10, decision_reason="r",
                  rationale="x", status="placed")
    f.books["council"]["open"].append({"tk": "Z-1", "contracts": 10, "entry": 0.50,
                                        "fee": 0.02, "ttl": 9_999})
    return f

def test_exit_eval_closes_position_on_take_profit():
    f = _armed_floor_with_open_yes()
    # yes bid rose to 0.58 -> gain 0.08 >= default 0.05 take-profit
    f._market_data = FakeMD(Market("Z-1", "t", yes_price=0.59, yes_bid=0.58, yes_ask=0.60))
    f.trader = FakeTrader()                      # fills the sell
    f._exit_eval()
    row = f.journal.get(1)
    assert row["status"] == "exited"
    assert row["realized_pnl"] > 0                       # closed at a profit
    assert f.journal.open_positions() == []
    assert f.books["council"]["open"] == []             # exposure freed

def test_exit_eval_holds_when_no_trigger():
    f = _armed_floor_with_open_yes()
    f._market_data = FakeMD(Market("Z-1", "t", yes_price=0.51, yes_bid=0.50, yes_ask=0.52))
    f.trader = FakeTrader()
    f._exit_eval()
    assert f.journal.open_positions() and f.journal.get(1)["status"] == "placed"

def test_exit_eval_does_not_consult_the_risk_guard():
    # The exit path must never call guard.check (closing only reduces exposure). Make the
    # guard raise if touched, then confirm the exit still completes.
    f = _armed_floor_with_open_yes()
    def _boom(*a, **k): raise AssertionError("exit path must not call guard.check")
    f.guard.check = _boom
    f._market_data = FakeMD(Market("Z-1", "t", yes_price=0.59, yes_bid=0.58, yes_ask=0.60))
    f.trader = FakeTrader()
    f._exit_eval()
    assert f.journal.get(1)["status"] == "exited"               # exit not gated by the guard
```

Simplify the profit assertion in `test_exit_eval_closes_position_on_take_profit` to just `assert row["realized_pnl"] > 0` (drop the `row_exit_fee` helper line — it is illustrative only). The exact fee comes from `kalshi_fee`; the test only needs to confirm a profitable close.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_floor_council.py -q -k exit_eval`
Expected: FAIL — `AttributeError: 'FloorState' object has no attribute '_exit_eval'`.

- [ ] **Step 3: Write the implementation**

(a) In `FloorState.__init__`, alongside the scout config block (~line 97), add:

```python
        # Exit layer — cheap, deterministic position management (no LLM calls).
        from .exits import ExitParams
        _sl = os.environ.get("EXIT_STOP_LOSS")
        self._exit_params = ExitParams(
            take_profit=float(os.environ.get("EXIT_TAKE_PROFIT", 0.05)),
            exit_edge=float(os.environ.get("EXIT_EDGE", 0.01)),
            stop_loss=float(_sl) if _sl else None,
        )
```

(b) Add `_exit_eval` and `_place_exit` methods (place them right after `_auto_execute`):

```python
    def _exit_eval(self) -> None:
        """Each tick: price every open real position off a fresh quote and close it if a
        trigger fires. Bypasses the risk guard — closing only reduces exposure."""
        if not (self.live and self.execute and self.journal and not self.frozen):
            return
        md = self._md()
        if not md:
            return
        from .exits import OpenPosition, exit_signal
        for row in self.journal.open_positions():
            m = md.get_market(row["market_id"])
            if m is None:                       # can't price it this tick — skip
                continue
            pos = OpenPosition(trade_id=row["id"], market_id=row["market_id"], side=row["side"],
                               entry_price=row["fill_price"], fair_value=row["converged_p"],
                               contracts=row["contracts"], entry_fee=row["fee"] or 0.0)
            sig = exit_signal(pos, m, self._exit_params)
            if sig.should_exit:
                self._place_exit(pos, m, sig)

    def _place_exit(self, pos, m, sig) -> None:
        """Cross the spread to SELL the side we hold, book the realized P&L, free exposure."""
        if pos.side == "yes":
            cross = round((m.yes_bid or sig.exit_price) * 100)
        else:                                   # selling NO -> hit the NO bid (= 1 - yes_ask)
            cross = round((1 - m.yes_ask if m.yes_ask else (1 - sig.exit_price)) * 100)
        cross = min(99, max(1, int(cross)))
        try:
            resp = self.trader.place_order(pos.market_id, pos.side, pos.contracts, cross, action="sell")
        except Exception as exc:  # noqa: BLE001 — never let a broker error crash the loop
            self._log(f"EXIT ORDER FAILED — {pos.market_id}: {type(exc).__name__}")
            return
        filled = int(float((resp or {}).get("fill_count", 0) or 0))
        if filled <= 0:
            self._log(f"EXIT NO FILL — {pos.side.upper()} {pos.market_id} (limit didn't cross)")
            return
        yes_px = float((resp or {}).get("average_fill_price", cross / 100.0) or cross / 100.0)
        exit_px = yes_px if pos.side == "yes" else round(1 - yes_px, 4)
        realized = self.journal.record_exit(pos.trade_id, exit_px, kalshi_fee(exit_px, filled), filled)
        self.books["council"]["open"] = [p for p in self.books["council"]["open"]
                                         if p["tk"] != pos.market_id]
        self._mirror(pos.trade_id)
        self._log(f"EXIT FILLED — {pos.side.upper()} {filled} {pos.market_id} @ "
                  f"{round(exit_px * 100)}¢ ({sig.reason}) realized ${realized:.2f}")
```

(c) In `tick`, add the exit pass right after the deliberation block (after the `if self._tick_n % self.DELIBERATE_EVERY == 0:` block, before the mark block):

```python
        if self.execute:
            self._exit_eval()                  # close positions that hit take-profit / edge-decay
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_floor_council.py -q`
Expected: PASS (existing floor tests + the 3 new exit tests).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (no regressions across the whole suite).

- [ ] **Step 6: Commit**

```bash
git add src/council/trading/floor.py tests/test_floor_council.py
git commit -m "feat(floor): per-tick exit eval — close on take-profit/edge-decay, guard-bypassed"
```

---

### Task 5: Demo-host sell verification (hard gate before live)

**Files:**
- Create: `scripts/verify_sell_demo.py` (one-off verification, not a pytest)

**Interfaces:**
- Consumes: `KalshiTrader` with `host=KalshiTrader.DEMO`; demo credentials in env (`KALSHI_DEMO_API_KEY_ID`, `KALSHI_DEMO_PRIVATE_KEY_PATH`).
- Produces: confirmation that the `action="sell"` body is accepted and fills on Kalshi's demo exchange.

This task exists because the sell wire-format is the only order shape not already verified against real fills. **Do not enable live exits until this passes.** It needs Kalshi demo-account credentials; if they aren't available, treat enabling live exits as blocked and tell the user.

- [ ] **Step 1: Write the verification script**

```python
# scripts/verify_sell_demo.py
"""Round-trip a 1-contract YES position on the Kalshi DEMO exchange to prove the
action='sell' close body is accepted and fills. Run before enabling live exits.

Env: KALSHI_DEMO_API_KEY_ID, KALSHI_DEMO_PRIVATE_KEY_PATH, and TICKER (a liquid demo market).
"""
import os, sys
from council.trading.execution import KalshiTrader

kid = os.environ.get("KALSHI_DEMO_API_KEY_ID")
pk = os.environ.get("KALSHI_DEMO_PRIVATE_KEY_PATH")
ticker = os.environ.get("TICKER")
if not (kid and pk and ticker):
    sys.exit("set KALSHI_DEMO_API_KEY_ID, KALSHI_DEMO_PRIVATE_KEY_PATH, TICKER")

t = KalshiTrader(kid, pk, host=KalshiTrader.DEMO)
print("buy  ->", t.place_order(ticker, "yes", 1, 60, action="buy"))
print("sell ->", t.place_order(ticker, "yes", 1, 40, action="sell"))  # close: cross down into the bid
```

- [ ] **Step 2: Run it against the demo exchange**

Run: `python scripts/verify_sell_demo.py`
Expected: both calls return a JSON order response with no HTTP error; the sell response shows `fill_count >= 1` (or a resting/accepted status). If the sell body is rejected, capture the error body, correct the `action="sell"` shape in `build_order` (Task 3) per Kalshi's response, re-run Task 3's unit tests, and repeat.

- [ ] **Step 3: Commit the verification script**

```bash
git add scripts/verify_sell_demo.py
git commit -m "chore(exits): demo-host sell verification script (pre-live gate)"
```

---

## Notes / known limitations (carried from the spec)

- **No hard downside cap by default.** Edge-decay never fires on an adverse move (a falling price looks like *more* edge), so a wrong call still rides to settlement. `EXIT_STOP_LOSS` is implemented but unset by default; set it (e.g. `EXIT_STOP_LOSS=0.10`) to cap downside.
- **Take-profit usually fires before full convergence** (5¢ gain vs reaching fair value) — deliberately conservative; tune `EXIT_TAKE_PROFIT` / `EXIT_EDGE`.
- **Partial exit fills:** the close order is IOC for the full count; `record_exit` books the filled count and marks the row `exited`. Positions are ≤ `MAX_POSITION_USD` ($5), so a partial in a liquid market is rare; any residual reconciles at settlement. Not handled further in this build.
- **Quote source:** `_exit_eval` fetches a fresh quote per open position via `self._md().get_market()` — the same Kalshi REST path `mark_open()` already uses. This is a REST GET, not an LLM call, so it honors the spec's "no token cost in the exit loop" intent.
- **Orphan positions** (pre-existing Kalshi positions the bot never opened) are out of scope — `open_positions()` only sees rows the bot logged.

## Self-Review

- **Spec coverage:** exits.py pure core (Task 1) ✓; edge-decay + take-profit + disabled stop-loss (Task 1) ✓; journal open/exit lifecycle (Task 2) ✓; sell path on the trader (Task 3) ✓; floor wiring + guard-bypass + exposure-free + Obsidian mirror (Task 4) ✓; cheap price-math eval / no LLM (Task 4 quote-via-REST note) ✓; env params (Task 4) ✓; demo verification for the unverified sell shape (Task 5) ✓; YAGNI exclusions (orphans, time-stop, council re-run) honored ✓.
- **Placeholder scan:** none — every step has full code or an exact command. (The one illustrative assertion line in Task 4 Step 1 is explicitly flagged to be simplified to `realized_pnl > 0`.)
- **Type consistency:** `OpenPosition`/`ExitParams`/`ExitDecision`/`exit_signal` names match across Tasks 1 and 4; `open_positions`/`record_exit` signatures match across Tasks 2 and 4; `place_order(..., action=...)` matches across Tasks 3, 4, and 5; all money in dollars except `limit_price_cents`.
