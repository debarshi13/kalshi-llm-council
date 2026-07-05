# Blend, Don't Beat — Edge Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-aim the Kalshi floor at markets where LLM signal can plausibly beat the price (tails, under-followed, maker-viable spreads), de-anchor the debate, execute as a maker, and lock live arming behind a Brier calibration gate.

**Architecture:** Pure scoring/fee/calibration functions in small new modules (`selection.py`, `calibrate.py`, additions to `ledger.py`); the debate in `deliberation.py` gains a blind round; `journal.py` gains a `blind_p` column, skip-resolution, and `calibration_report()`; `execution.py` gains resting (GTC) orders + `get_order`/`cancel_order`; `floor.py` swaps taker crossing for a maker order lifecycle (place → poll → fill/TTL-cancel) shared by paper and real modes.

**Tech Stack:** Python 3.14, sqlite3, httpx (lazy), LiteLLM via existing `ModelClient`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-04-blend-dont-beat-design.md`

## Global Constraints

- Python 3.14; no new runtime dependencies.
- Full test suite must stay green **offline, with no API keys** (`pytest -q` from repo root, venv at `.venv`).
- RiskGuard caps and the exit layer (`exits.py`) are unchanged.
- All new tunables are env vars with defaults, read in `FloorState.__init__` (existing pattern): `EDGE_HORIZON_DAYS=7`, `EXTREMIZE_ALPHA=1.3`, `SPREAD_BUFFER=0.01`, `MIN_PROFIT=0.01`, `ORDER_TTL_TICKS=30`, `CALIBRATION_MIN_N=50`, `COUNCIL_CALIBRATION_OVERRIDE=false`. Flat `EDGE_THRESHOLD` is removed.
- Never let a broker/pricing error crash the tick loop (existing `# noqa: BLE001` pattern).
- Real-order code paths (`tif="gtc"`, `get_order`, `cancel_order`) must be verified against the Kalshi **demo host** before any armed run (same pattern as `scripts/verify_sell_demo.py`); the plan builds them behind mocks.

---

### Task 1: Fee-aware edge gate (`ledger.py`)

**Files:**
- Modify: `src/council/trading/ledger.py` (append after `kalshi_fee`, line 25)
- Test: `tests/test_edge_gate.py` (create)

**Interfaces:**
- Consumes: `kalshi_fee(price, contracts, rate=0.07)` (existing).
- Produces: `maker_fee(price: float, contracts: int) -> float` (dollars, 25% of taker, ceil-to-cent) and `required_edge(price: float, contracts: int = 10, spread_buffer: float = 0.01, min_profit: float = 0.01) -> float` (per-contract required edge in dollars). Task 5's `decide()` calls `required_edge`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_edge_gate.py
from council.trading.ledger import kalshi_fee, maker_fee, required_edge


def test_maker_fee_is_quarter_of_taker_before_rounding():
    # 10 contracts at 50c: taker raw = 17.5c -> maker raw = 4.375c -> ceil 5c
    assert maker_fee(0.50, 10) == 0.05
    # 10 contracts at 90c: taker raw = 6.3c -> maker raw = 1.575c -> ceil 2c
    assert maker_fee(0.90, 10) == 0.02


def test_required_edge_cheaper_in_tails_than_at_midprice():
    mid = required_edge(0.50)
    tail = required_edge(0.90)
    assert tail < mid
    # floor: always at least spread_buffer + min_profit
    assert tail >= 0.02


def test_required_edge_components():
    # at 0.90, 10 contracts: per-contract maker fee = 0.02/10 = 0.002
    assert abs(required_edge(0.90, 10, 0.01, 0.01) - (0.002 + 0.01 + 0.01)) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_edge_gate.py -v`
Expected: FAIL — `ImportError: cannot import name 'maker_fee'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/council/trading/ledger.py`:

```python
MAKER_RATE_FRACTION = 0.25   # Kalshi maker fee = 25% of the taker rate


def maker_fee(price: float, contracts: int, rate: float = 0.07) -> float:
    """Maker-side Kalshi fee in dollars: 25% of the taker formula, ceil to next cent."""
    raw_cents = MAKER_RATE_FRACTION * rate * contracts * price * (1.0 - price) * 100
    return math.ceil(round(raw_cents, 6)) / 100.0


def required_edge(price: float, contracts: int = 10, spread_buffer: float = 0.01,
                  min_profit: float = 0.01) -> float:
    """Per-contract edge (dollars) a maker entry at `price` must clear to be worth placing.

    Fee is amortized over a nominal `contracts` size so the ceil-to-cent floor
    doesn't flatten the tails-vs-mid fee difference the strategy depends on.
    Settlement is fee-free on Kalshi, so only the entry fee is charged here.
    """
    return maker_fee(price, contracts) / max(contracts, 1) + spread_buffer + min_profit
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_edge_gate.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/ledger.py tests/test_edge_gate.py
git commit -m "feat(fees): maker_fee + fee-aware required_edge gate"
```

---

### Task 2: Market selection — `edge_score` + wiring (`selection.py`, `floor.py`, `scout.py`)

**Files:**
- Create: `src/council/trading/selection.py`
- Modify: `src/council/trading/floor.py:242-272` (`_load_live_markets`, `cheap_score`), `src/council/trading/scout.py:19-46` (`structural_score`)
- Test: `tests/test_selection.py` (create)

**Interfaces:**
- Consumes: `Market` dataclass (`market.py`): fields `yes_price, volume, close_ts, yes_bid, yes_ask`.
- Produces: `SelectionParams` dataclass and `edge_score(m: Market, params: SelectionParams | None = None, now: float | None = None) -> float` (higher = better hunting ground; `-inf` = untradeable). `maker_price_cents(m: Market, side: str) -> int | None` (inside-spread resting price; `None` when no book). Tasks 5 and 8 consume `maker_price_cents`; `scout.structural_score` and `floor._load_live_markets` consume `edge_score`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_selection.py
import time
import math
from council.trading.market import Market
from council.trading.selection import SelectionParams, edge_score, maker_price_cents


def _mk(price, volume, hrs=48.0, bid=None, ask=None):
    return Market("KXTEST-A", "t", price, volume=volume,
                  close_ts=int(time.time() + hrs * 3600),
                  yes_bid=bid or 0.0, yes_ask=ask or 0.0)


def test_tail_beats_midprice_all_else_equal():
    assert edge_score(_mk(0.90, 500)) > edge_score(_mk(0.50, 500))


def test_underfollowed_beats_heavy_volume():
    assert edge_score(_mk(0.90, 500)) > edge_score(_mk(0.90, 100_000))


def test_below_min_volume_is_untradeable():
    assert edge_score(_mk(0.90, 10)) == -math.inf


def test_subhour_lottery_excluded():
    assert edge_score(_mk(0.90, 500, hrs=0.5)) == -math.inf


def test_beyond_horizon_excluded():
    assert edge_score(_mk(0.90, 500, hrs=24 * 30)) == -math.inf


def test_maker_viable_spread_beats_no_book():
    with_book = _mk(0.90, 500, bid=0.87, ask=0.93)
    without = _mk(0.90, 500)
    assert edge_score(with_book) > edge_score(without)


def test_maker_price_sits_inside_spread():
    m = _mk(0.90, 500, bid=0.87, ask=0.93)
    assert maker_price_cents(m, "yes") == 88          # bid+1c
    assert maker_price_cents(m, "no") == 100 - 92     # NO resting = 1 - (ask-1c)


def test_maker_price_none_without_book():
    assert maker_price_cents(_mk(0.90, 500), "yes") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_selection.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'council.trading.selection'`

- [ ] **Step 3: Write minimal implementation**

Create `src/council/trading/selection.py`:

```python
"""Market selection: score markets by how plausibly the price is WEAK.

Inverts the old cheap_score liquidity bias. We hunt tails (cheap fees +
favorite-longshot bias), under-followed volume (price ~ raw crowd, not yet
corrected by sharps), maker-viable spreads, and horizons short enough that
calibration data accrues — but never sub-hour lotteries.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .market import Market


@dataclass(frozen=True)
class SelectionParams:
    min_volume: int = 50          # below this, fills are unreliable (existing MIN_VOLUME)
    followed_volume: int = 20_000  # volume at which a market counts as fully "followed"
    min_spread: float = 0.02      # narrower than this leaves no room to post inside
    max_spread: float = 0.15      # wider than this, resting fills are unrealistic
    horizon_days: float = 7.0     # resolve soon enough that calibration accrues
    min_hours: float = 1.0        # exclude sub-hour lotteries (BTC hourly strikes)


def edge_score(m: Market, params: SelectionParams | None = None,
               now: float | None = None) -> float:
    """Composite 0..3 score; -inf = untradeable. Higher = weaker price, better target."""
    p = params or SelectionParams()
    now = now or time.time()
    if m.volume < p.min_volume:
        return -math.inf
    hrs = (m.close_ts - now) / 3600.0 if m.close_ts else None
    if hrs is not None and (hrs < p.min_hours or hrs > p.horizon_days * 24.0):
        return -math.inf
    tail = abs(m.yes_price - 0.5) * 2.0                                   # 0 mid .. 1 extreme
    under = 1.0 - min(m.volume / p.followed_volume, 1.0)                  # 1 thin .. 0 heavy
    spread = (m.yes_ask - m.yes_bid) if (m.yes_ask and m.yes_bid) else 0.0
    if p.min_spread <= spread <= p.max_spread:
        viability = 1.0
    elif spread > 0.0:
        viability = 0.3      # book exists but too tight/too wide to post inside
    else:
        viability = 0.0      # no book at all
    return tail + under + viability


def maker_price_cents(m: Market, side: str) -> int | None:
    """Resting limit inside the spread, in the side's own cents. None = no two-sided book."""
    if not (m.yes_bid and m.yes_ask):
        return None
    bid, ask = round(m.yes_bid * 100), round(m.yes_ask * 100)
    if ask - bid < 2:                       # no room to post inside
        return None
    if side == "yes":
        yes_px = bid + 1
    else:
        yes_px = ask - 1                    # improving the YES ask == bidding for NO
    yes_px = min(99, max(1, yes_px))
    return yes_px if side == "yes" else 100 - yes_px
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_selection.py -v`
Expected: 8 passed

- [ ] **Step 5: Wire into floor and scout**

In `src/council/trading/floor.py`:
- Add import: `from .selection import SelectionParams, edge_score, maker_price_cents`
- In `__init__`, after the exit-params block, add:

```python
        # Selection: hunt weak prices (tails, under-followed, maker-viable spreads).
        self._sel_params = SelectionParams(
            min_volume=self.MIN_VOLUME,
            horizon_days=float(os.environ.get("EDGE_HORIZON_DAYS", 7)),
        )
```

- In `_load_live_markets`, replace the filter/sort lines:

```python
            mk = [m for m in raw if 0.03 <= m.yes_price <= 0.97]
            picks = sorted((m for m in mk if edge_score(m, self._sel_params) > float("-inf")),
                           key=lambda m: edge_score(m, self._sel_params), reverse=True)[:20]
```

and update the two log strings from "liquid-market focus" to "weak-price focus" and the no-picks message to `f"⚠ NO TRADEABLE LIVE MARKETS — {len(raw)} fetched, none cleared edge_score gates (vol≥{self.MIN_VOLUME}, price 0.03–0.97, horizon)"`.

- Replace the body of `cheap_score` (kept as the scout-disabled fallback ranker, used at `floor.py:307`):

```python
    def cheap_score(self, m) -> float:
        """Fallback ranking when the scout is disabled — same weak-price scoring."""
        return edge_score(m, self._sel_params)
```

In `src/council/trading/scout.py`, replace the entire body of `structural_score` with a delegation (keeps the scout/floor rankings consistent, DRY):

```python
def structural_score(m: Market) -> float:
    """Tier-0 signal: delegate to selection.edge_score (weak-price composite)."""
    from .selection import edge_score
    return edge_score(m)
```

- [ ] **Step 6: Run full suite; fix ranking-dependent tests**

Run: `cd /home/debarshi/council && .venv/bin/pytest -q`
Expected: `tests/test_scout.py` tests asserting old `structural_score` component math will FAIL. Update those tests to assert the new behavior (tail + thin volume + maker-viable spread outranks mid-price + heavy volume), e.g. replace component assertions with:

```python
def test_structural_score_prefers_weak_prices():
    weak = Market("KXA-1", "t", 0.90, volume=500,
                  close_ts=int(time.time() + 48 * 3600), yes_bid=0.87, yes_ask=0.93)
    strong = Market("KXB-1", "t", 0.50, volume=100_000,
                    close_ts=int(time.time() + 48 * 3600), yes_bid=0.49, yes_ask=0.51)
    assert structural_score(weak) > structural_score(strong)
```

`tests/test_floor_council.py` tests referencing `cheap_score`'s volume/hour math: update to construct markets with `close_ts` within horizon and assert ordering by `edge_score`. Re-run until green.

- [ ] **Step 7: Commit**

```bash
git add src/council/trading/selection.py src/council/trading/floor.py src/council/trading/scout.py tests/
git commit -m "feat(selection): weak-price edge_score replaces liquidity-biased cheap_score"
```

---

### Task 3: Calibration math (`calibrate.py`)

**Files:**
- Create: `src/council/trading/calibrate.py`
- Test: `tests/test_calibrate.py` (create)

**Interfaces:**
- Produces: `extremize(p: float, alpha: float = 1.3) -> float` (logit-space extremization, clamped to (0,1)) and `brier(p: float, outcome: int) -> float`. Task 4 (deliberation) consumes `extremize`; Task 6 (journal) consumes `brier`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calibrate.py
from council.trading.calibrate import brier, extremize


def test_extremize_pushes_away_from_half():
    assert extremize(0.70, 1.3) > 0.70
    assert extremize(0.30, 1.3) < 0.30


def test_extremize_fixed_points_and_identity():
    assert extremize(0.5, 1.3) == 0.5
    assert abs(extremize(0.7, 1.0) - 0.7) < 1e-9


def test_extremize_clamps_extremes():
    assert 0.0 < extremize(0.999, 2.0) < 1.0
    assert 0.0 < extremize(0.001, 2.0) < 1.0


def test_brier():
    assert brier(0.8, 1) == round((0.8 - 1) ** 2, 10)
    assert brier(0.8, 0) == round(0.8 ** 2, 10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_calibrate.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Create `src/council/trading/calibrate.py`:

```python
"""Probability calibration helpers — pure math, no I/O.

extremize() counters documented LLM underconfidence (KalshiBench: models say
50% when the truth is ~80%) by scaling in logit space. alpha=1 is identity;
alpha>1 pushes estimates away from 0.5. Fitted later from journal buckets.
"""
from __future__ import annotations

import math

_EPS = 1e-6


def extremize(p: float, alpha: float = 1.3) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    logit = math.log(p / (1.0 - p)) * alpha
    return 1.0 / (1.0 + math.exp(-logit))


def brier(p: float, outcome: int) -> float:
    """Squared error of probability p against outcome (1=YES, 0=NO). Lower is better."""
    return round((p - float(outcome)) ** 2, 10)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_calibrate.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/calibrate.py tests/test_calibrate.py
git commit -m "feat(calibrate): logit-space extremization + brier helpers"
```

---

### Task 4: Blind-then-converge debate (`deliberation.py`)

**Files:**
- Modify: `src/council/trading/deliberation.py:79-172` (prompts, `DeliberativeCouncil.debate`, `MockCouncil.debate`)
- Test: `tests/test_deliberation.py` (extend)

**Interfaces:**
- Consumes: `ModelClient.complete(spec, messages) -> tuple[str, Usage]`; `extremize` from Task 3.
- Produces: `Deliberation` with **both** `round1` (blind estimates) and `round2` (post-reveal) populated; `converged_p` = extremized median of round-2; `DeliberativeCouncil.__init__(specs, client, alpha: float = 1.3)`. The floor (Task 9) and `_journal_log` rely on `d.round1` being non-empty; `blind_p` for the journal = `statistics.median(e.p_yes for e in d.round1)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deliberation.py` (reuse the file's existing `FakeClient`-style helper if present; otherwise this self-contained fake):

```python
import statistics
from council.trading.deliberation import DeliberativeCouncil
from council.trading.market import Market
from council.models import ModelSpec


class ScriptedClient:
    """Returns scripted texts in order; records every prompt it saw."""
    def __init__(self, texts):
        self.texts = list(texts)
        self.prompts = []

    def complete(self, spec, messages):
        self.prompts.append((messages[0]["content"], messages[1]["content"]))
        return self.texts.pop(0), None


def _mkt():
    return Market("KXQ-1", "Will it rain?", 0.80, volume=500,
                  yes_bid=0.78, yes_ask=0.82, rules="Resolves YES if NWS reports rain.")


def test_round1_is_blind_no_price_no_peers():
    texts = ["A. P(YES): 0.60", "B. P(YES): 0.70", "C. P(YES): 0.65",
             "A2. P(YES): 0.62", "B2. P(YES): 0.70", "C2. P(YES): 0.66"]
    client = ScriptedClient(texts)
    council = DeliberativeCouncil([ModelSpec("openrouter/anthropic/claude-opus-4.8", "x"),
                                   ModelSpec("openrouter/moonshotai/kimi-k2.6", "x"),
                                   ModelSpec("openrouter/openai/gpt-5.4", "x")], client)
    d = council.debate(_mkt(), "some research")
    # first 3 calls are round 1: no market price anywhere in the prompt
    for sys_msg, user_msg in client.prompts[:3]:
        assert "0.80" not in sys_msg and "0.80" not in user_msg
        assert "P(YES): 0.6" not in sys_msg          # no peer estimates leaked
    # last 3 calls are round 2: price and peers revealed
    for sys_msg, user_msg in client.prompts[3:]:
        assert "0.80" in sys_msg or "0.80" in user_msg
    assert len(d.round1) == 3 and len(d.round2) == 3


def test_converged_is_extremized_median_of_round2():
    texts = ["P(YES): 0.60", "P(YES): 0.70", "P(YES): 0.65",
             "P(YES): 0.10", "P(YES): 0.70", "P(YES): 0.72"]   # 0.10 outlier
    client = ScriptedClient(texts)
    council = DeliberativeCouncil([ModelSpec("m/a", "x"), ModelSpec("m/b", "x"),
                                   ModelSpec("m/c", "x")], client, alpha=1.0)
    d = council.debate(_mkt(), "notes")
    assert d.converged_p == 0.70          # median ignores the outlier; alpha=1 = no shift


def test_mock_council_populates_round1():
    from council.trading.deliberation import MockCouncil
    d = MockCouncil(["A", "B", "C"]).debate(_mkt(), "notes")
    assert len(d.round1) == 3 and len(d.round2) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_deliberation.py -v`
Expected: new tests FAIL (round1 empty; converged is mean of a single sequential round)

- [ ] **Step 3: Write the implementation**

In `src/council/trading/deliberation.py`:

Add import at top: `from .calibrate import extremize`

Replace `_ROUNDTABLE_SYS` with two prompts:

```python
_BLIND_SYS = (
    "You are {name}, a sharp prediction-market analyst. Estimate the probability that "
    "the market below resolves YES, using ONLY the resolution rules, the research notes, "
    "and your own knowledge. You are deliberately NOT shown the market price — form an "
    "independent view. Read the resolution rules carefully (misreading the threshold or "
    "direction is the most common, costly error). 2-3 sentences of reasoning, then END "
    "with a line exactly:\nP(YES): <number between 0 and 1>"
)

_CONVERGE_SYS = (
    "You are {name}, at a roundtable with two other analysts. You all just estimated this "
    "market blind; now the market price and everyone's blind estimates are revealed. The "
    "price is ONE input: it reflects the crowd, but thin or under-followed books can be "
    "stale or biased. Revise your estimate only for a SPECIFIC stated reason (a rule "
    "misread, information a colleague raised, or a crowd bias you can name) — do not "
    "reflexively defer to the price, and do not move just to agree. 2-3 sentences, then "
    "END with a line exactly:\nP(YES): <number between 0 and 1>"
)
```

Add a blind market block (no price, no quote) next to `_market_block`:

```python
def _blind_block(market: Market, notes: str, lessons: str = "") -> str:
    rules = f"Resolution rules: {market.rules}\n" if market.rules else ""
    les = f"{lessons}\n" if lessons else ""
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"{rules}{les}Research notes:\n{notes}\n")
```

Add a no-anchor parser next to `_parse_prob` (the existing `_parse_prob` falls back to the market price via `parse_estimate` — that would re-anchor the blind round):

```python
def _parse_prob_blind(text: str) -> float | None:
    """Blind-round parser: NEVER falls back to the market price."""
    matches = _PYES_RE.findall(text)
    if not matches:
        return None
    val, pct = matches[-1]
    p = float(val)
    if pct or p > 1:
        p /= 100.0
    return min(max(p, 0.0), 1.0)
```

Replace `DeliberativeCouncil` with:

```python
class DeliberativeCouncil:
    def __init__(self, specs: list[ModelSpec], client: ModelClient, alpha: float = 1.3) -> None:
        self.specs = specs            # speaking order; the most capable model should be last
        self.client = client
        self.alpha = alpha            # extremization strength (EXTREMIZE_ALPHA)

    def debate(self, market: Market, notes: str, lessons: str = "") -> Deliberation:
        """Round 1: every model estimates BLIND (no price, no peers) — independent signal.
        Round 2: price + all blind estimates revealed; models may revise with a reason.
        Converged = extremized MEDIAN of round 2 (median resists one outlier model)."""
        blind = _blind_block(market, notes, lessons)
        round1: list[ModelEstimate] = []
        for spec in self.specs:
            name = _name_for(spec.model)
            system = _BLIND_SYS.format(name=name) + "\n\n" + blind
            text, _ = self.client.complete(
                spec, [{"role": "system", "content": system},
                       {"role": "user", "content": f"You are {name}. Give your independent estimate."}])
            msg = text.strip()
            p = _parse_prob_blind(msg)
            if p is not None:                       # unparseable blind turn adds no signal
                round1.append(ModelEstimate(name, p, msg))

        reveal = _market_block(market, notes, lessons) + "\nBlind estimates:\n" + \
            "\n".join(f"- {e.model}: P(YES) {e.p_yes:.2f} — {e.thesis}" for e in round1)
        round2: list[ModelEstimate] = []
        for spec in self.specs:
            name = _name_for(spec.model)
            system = _CONVERGE_SYS.format(name=name) + "\n\n" + reveal
            text, _ = self.client.complete(
                spec, [{"role": "system", "content": system},
                       {"role": "user", "content": f"You are {name}. Give your final estimate."}])
            msg = text.strip()
            round2.append(ModelEstimate(name, _parse_prob(msg, market), msg))

        ps = [t.p_yes for t in round2]
        converged = extremize(statistics.median(ps), self.alpha)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, round1, round2, round(converged, 4), round(spread, 4), notes)
```

Update `MockCouncil.debate` to emit both rounds (round 1 scattered around 0.5, round 2 near price — mirrors real anchoring dynamics for the UI):

```python
    def debate(self, market: Market, notes: str, lessons: str = "") -> Deliberation:
        import random
        round1 = [ModelEstimate(n, round(min(max(0.5 + random.gauss(0, 0.15), 0.02), 0.98), 3),
                                f"{n}: blind take.") for n in self.specs]
        round2 = []
        for name in self.specs:
            p = round(min(max(market.yes_price + random.gauss(0, 0.08), 0.02), 0.98), 3)
            round2.append(ModelEstimate(name, p, f"{name}: I read this around {p:.0%}. P(YES): {p:.2f}"))
        ps = [t.p_yes for t in round2]
        converged = sum(ps) / len(ps)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, round1, round2, round(converged, 4), round(spread, 4), notes)
```

- [ ] **Step 4: Run tests**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_deliberation.py tests/test_mock_council.py -v`
Expected: new tests PASS. Existing tests asserting the old single-round call count (3 completes) or mean aggregation will FAIL — update them: call count is now `2 * len(specs)`, aggregation is extremized median (construct with `alpha=1.0` in tests that assert exact converged values).

- [ ] **Step 5: Full suite green, commit**

Run: `cd /home/debarshi/council && .venv/bin/pytest -q` — fix any residual assertion drift in `test_floor_council.py` (snapshot `round1` is now non-empty).

```bash
git add src/council/trading/deliberation.py tests/
git commit -m "feat(debate): blind round-1 + reveal round-2, extremized median consensus"
```

---

### Task 5: Fee-aware maker `decide()` (`deliberation.py`)

**Files:**
- Modify: `src/council/trading/deliberation.py:44-72` (`decide`)
- Test: `tests/test_deliberation.py` (extend)

**Interfaces:**
- Consumes: `required_edge` (Task 1), `maker_price_cents` (Task 2).
- Produces: new signature `decide(d, market, caps, spread_cap=0.08, spread_buffer=0.01, min_profit=0.01, full_conviction_edge=0.20) -> Decision`. Edge is measured against the **maker entry price** (inside the spread), and the threshold is `required_edge(entry, contracts=10, spread_buffer, min_profit)`. `Decision.limit_price_cents` is the maker resting price in the side's own cents. Task 9's floor call site passes `spread_buffer`/`min_profit` from env. The `edge_threshold` parameter is **removed**.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_deliberation.py
from council.trading.deliberation import Deliberation, ModelEstimate, decide
from council.trading.execution import RiskGuard


def _delib(p, spread=0.01):
    est = [ModelEstimate("A", p, "t")]
    return Deliberation("KXQ-1", est, est, p, spread)


def test_decide_uses_maker_price_inside_spread():
    m = Market("KXQ-1", "t", 0.80, volume=500, yes_bid=0.78, yes_ask=0.82)
    dec = decide(_delib(0.90), m, RiskGuard())
    assert dec.place and dec.side == "yes"
    assert dec.limit_price_cents == 79     # bid 78 + 1, NOT the 82 ask


def test_decide_fee_aware_threshold_blocks_thin_edge():
    m = Market("KXQ-1", "t", 0.80, volume=500, yes_bid=0.78, yes_ask=0.82)
    dec = decide(_delib(0.81), m, RiskGuard())   # 2c edge vs 79c entry < required
    assert not dec.place
    assert "required" in dec.reason


def test_decide_skips_when_no_book():
    m = Market("KXQ-1", "t", 0.80, volume=500)   # no bid/ask
    dec = decide(_delib(0.90), m, RiskGuard())
    assert not dec.place and "book" in dec.reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_deliberation.py -k decide -v`
Expected: FAIL (old decide pays the ask and uses flat 6c default threshold)

- [ ] **Step 3: Write the implementation**

Replace `decide` in `src/council/trading/deliberation.py` (imports at top: `from .ledger import required_edge` and `from .selection import maker_price_cents`):

```python
def decide(d: Deliberation, market: Market, caps: RiskGuard,
           spread_cap: float = 0.05, spread_buffer: float = 0.01,
           min_profit: float = 0.01, full_conviction_edge: float = 0.20) -> Decision:
    """Maker-entry decision: post inside the spread; edge must clear the per-price fee gate."""
    yes_cents = maker_price_cents(market, "yes")
    no_cents = maker_price_cents(market, "no")
    if yes_cents is None or no_cents is None:
        return Decision(False, None, 0, 0, "no two-sided book to post inside -> SKIP")
    yes_entry, no_entry = yes_cents / 100.0, no_cents / 100.0
    yes_edge = d.converged_p - yes_entry            # buy YES resting at bid+1
    no_edge = (1.0 - d.converged_p) - no_entry      # buy NO resting at (1-ask)+1
    if yes_edge >= no_edge:
        side, edge, entry, price_cents = "yes", yes_edge, yes_entry, yes_cents
    else:
        side, edge, entry, price_cents = "no", no_edge, no_entry, no_cents
    if d.spread > spread_cap:
        return Decision(False, None, 0, price_cents,
                        f"no consensus (spread {d.spread:.3f} > {spread_cap:.3f})")
    need = required_edge(entry, 10, spread_buffer, min_profit)
    if edge < need:
        return Decision(False, None, 0, price_cents,
                        f"edge {edge*100:.1f}c < required {need*100:.1f}c (fee-aware @ {price_cents}c) -> SKIP")
    span = max(full_conviction_edge - need, 1e-9)
    conviction = min(1.0, (edge - need) / span)
    agreement = 1.0 - min(d.spread / spread_cap, 1.0)
    size_frac = 0.3 + 0.7 * conviction * agreement
    contracts = max(1, int((caps.max_position_usd * size_frac) / max(entry, 0.05)))
    return Decision(True, side, contracts, price_cents,
                    f"{edge*100:.1f}c {side.upper()} maker-edge (need {need*100:.1f}c), "
                    f"spread {d.spread:.3f}, size {size_frac*100:.0f}% -> PLACE")
```

- [ ] **Step 4: Run tests, fix callers**

Run: `cd /home/debarshi/council && .venv/bin/pytest -q`
Expected: new tests PASS; `floor.py:313` still passes `edge_threshold=` — update that call now (full wiring lands in Task 9):

```python
        dec = decide(d, m, self.guard, spread_cap=self.SPREAD_CAP,
                     spread_buffer=self.SPREAD_BUFFER, min_profit=self.MIN_PROFIT)
```

and in `FloorState.__init__` replace the `EDGE_THRESHOLD` line with:

```python
        self.SPREAD_BUFFER = float(os.environ.get("SPREAD_BUFFER", 0.01))
        self.MIN_PROFIT = float(os.environ.get("MIN_PROFIT", 0.01))
```

Old `decide` tests asserting taker-ask entries: update to two-sided-book fixtures and maker prices. Re-run until green.

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/deliberation.py src/council/trading/floor.py tests/
git commit -m "feat(decide): maker entry pricing + fee-aware per-price edge gate"
```

---

### Task 6: Journal — `blind_p`, skip resolution, order lifecycle, `calibration_report()` (`journal.py`)

**Files:**
- Modify: `src/council/trading/journal.py`
- Test: `tests/test_journal.py` (extend)

**Interfaces:**
- Consumes: `brier` (Task 3).
- Produces:
  - `log(..., blind_p: float | None = None, status="placed")` — new keyword.
  - `resolve(market_id, outcome)` now also resolves `status='skipped'` and `status='working'` rows → `'resolved_skip'` / `'cancelled'` respectively (predictions count; unfilled orders don't).
  - `mark_filled(trade_id: int, fill_price: float, fee: float, fill_count: int) -> None` — flips a `'working'` row to `'placed'` with real fill data.
  - `cancel(trade_id: int) -> None` — flips a `'working'` row to `'cancelled'`.
  - `calibration_report(min_n: int = 50) -> dict` with keys `n, brier_model, brier_market, brier_blind, beats_market` (floats or `None` when `n==0`; `beats_market` = `n >= min_n and brier_model < brier_market`). Task 10's arming gate consumes this.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_journal.py
from council.trading.journal import Journal


def _log(j, status, converged=0.9, market=0.8, blind=None, mid="KXC-1"):
    return j.log(market_id=mid, title="t", side="yes", converged_p=converged, spread=0.01,
                 market_price=market, executable_price=0.81, edge=converged - market,
                 contracts=5, fill_price=0.81 if status == "placed" else 0.0,
                 fee=0.01, fill_count=5 if status == "placed" else 0,
                 decision_reason="r", rationale="x", status=status, blind_p=blind)


def test_resolve_also_resolves_skipped_predictions():
    j = Journal(":memory:")
    _log(j, "skipped", converged=0.9, market=0.8)
    j.resolve("KXC-1", "yes")
    r = j._c.execute("SELECT status, outcome FROM trades").fetchone()
    assert r["status"] == "resolved_skip" and r["outcome"] == "yes"


def test_working_orders_cancel_on_resolution_and_never_score():
    j = Journal(":memory:")
    _log(j, "working")
    j.resolve("KXC-1", "no")
    assert j._c.execute("SELECT status FROM trades").fetchone()["status"] == "cancelled"
    assert j.calibration_report()["n"] == 0


def test_mark_filled_and_cancel_lifecycle():
    j = Journal(":memory:")
    tid = _log(j, "working")
    j.mark_filled(tid, 0.79, 0.02, 5)
    r = j.get(tid)
    assert r["status"] == "placed" and r["fill_price"] == 0.79 and r["fill_count"] == 5
    tid2 = _log(j, "working", mid="KXC-2")
    j.cancel(tid2)
    assert j.get(tid2)["status"] == "cancelled"


def test_calibration_report_brier_vs_market():
    j = Journal(":memory:")
    # model says 0.9, market 0.6, outcome YES -> model brier 0.01, market 0.16
    _log(j, "skipped", converged=0.9, market=0.6, blind=0.85)
    j.resolve("KXC-1", "yes")
    rep = j.calibration_report(min_n=1)
    assert rep["n"] == 1
    assert abs(rep["brier_model"] - 0.01) < 1e-9
    assert abs(rep["brier_market"] - 0.16) < 1e-9
    assert abs(rep["brier_blind"] - 0.0225) < 1e-9
    assert rep["beats_market"] is True


def test_calibration_report_needs_min_n():
    j = Journal(":memory:")
    _log(j, "skipped", converged=0.9, market=0.6)
    j.resolve("KXC-1", "yes")
    assert j.calibration_report(min_n=50)["beats_market"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_journal.py -v`
Expected: FAIL — `log() got an unexpected keyword argument 'blind_p'`

- [ ] **Step 3: Write the implementation**

In `src/council/trading/journal.py` (import at top: `from .calibrate import brier`):

In `__init__`, add `blind_p REAL` to the CREATE TABLE column list (after `converged_p REAL,`), then add a migration for existing DBs right after the CREATE:

```python
        try:                                   # migrate pre-existing journals in place
            self._c.execute("ALTER TABLE trades ADD COLUMN blind_p REAL")
        except sqlite3.OperationalError:
            pass                               # column already exists
        self._c.commit()
```

Update `log` signature and INSERT:

```python
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
```

Extend `resolve` — after the existing placed-rows loop, before `self._c.commit()`:

```python
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
```

Add the lifecycle + report methods (after `record_exit`):

```python
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
```

- [ ] **Step 4: Run tests**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_journal.py -v`
Expected: all pass (existing tests untouched — `blind_p` is keyword-optional)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/journal.py tests/test_journal.py
git commit -m "feat(journal): blind_p, skip/working resolution, fill lifecycle, calibration_report"
```

---

### Task 7: Resting orders on Kalshi (`execution.py`)

**Files:**
- Modify: `src/council/trading/execution.py:72-115` (`build_order`, `place_order`; add `get_order`, `cancel_order`)
- Test: `tests/test_execution.py` (extend)

**Interfaces:**
- Consumes: existing `_signed_headers`.
- Produces: `build_order(ticker, side, count, limit_price_cents, action="buy", tif="ioc") -> dict` (`tif="gtc"` omits `time_in_force` → resting order); `place_order(..., tif="ioc") -> dict`; `get_order(order_id: str) -> dict` (GET `/portfolio/orders/{id}`); `cancel_order(order_id: str) -> dict` (DELETE `/portfolio/orders/{id}`). Task 8's floor consumes all three. **Demo-host verification required before any armed run** — extend `scripts/verify_sell_demo.py`'s pattern with a `scripts/verify_maker_demo.py` that places a deep out-of-the-money resting order, polls it, cancels it.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_execution.py
import pytest
from council.trading.execution import KalshiTrader


def test_build_order_gtc_omits_time_in_force():
    body = KalshiTrader.build_order("KXT-1", "yes", 5, 79, tif="gtc")
    assert "time_in_force" not in body
    assert body["side"] == "bid" and body["price"] == "0.7900"


def test_build_order_default_stays_ioc():
    body = KalshiTrader.build_order("KXT-1", "yes", 5, 79)
    assert body["time_in_force"] == "immediate_or_cancel"


def test_build_order_rejects_bad_tif():
    with pytest.raises(ValueError):
        KalshiTrader.build_order("KXT-1", "yes", 5, 79, tif="whenever")


def test_order_status_paths():
    t = KalshiTrader("kid", "/tmp/nope.pem")
    assert t.ORDER_STATUS_PATH.format(order_id="abc") == "/portfolio/orders/abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_execution.py -v`
Expected: FAIL — `build_order() got an unexpected keyword argument 'tif'`

- [ ] **Step 3: Write the implementation**

In `src/council/trading/execution.py`:

Add class constant under `ORDER_PATH`:

```python
    ORDER_STATUS_PATH = "/portfolio/orders/{order_id}"   # GET = status, DELETE = cancel
```

`build_order` — add the `tif` parameter and validation (after the `action` check):

```python
    @staticmethod
    def build_order(ticker: str, side: str, count: int, limit_price_cents: int,
                    action: str = "buy", tif: str = "ioc") -> dict:
```
```python
        if tif not in ("ioc", "gtc"):
            raise ValueError(f"tif must be ioc/gtc, got {tif!r}")
```

and replace the body dict's `time_in_force` line — build the dict without it, then:

```python
        if tif == "ioc":
            body["time_in_force"] = "immediate_or_cancel"
        # tif="gtc": omit time_in_force -> order rests on the book until filled/cancelled.
        # VERIFY on the demo host before live (scripts/verify_maker_demo.py).
```

`place_order` — pass through:

```python
    def place_order(self, ticker: str, side: str, count: int, limit_price_cents: int,
                    action: str = "buy", tif: str = "ioc") -> dict:
```
(and `body = self.build_order(ticker, side, count, limit_price_cents, action, tif)`)

Add the two live methods after `place_order`:

```python
    def get_order(self, order_id: str) -> dict:
        """LIVE — order status incl. fill_count. GET /portfolio/orders/{id}."""
        import httpx

        path = self.PREFIX + self.ORDER_STATUS_PATH.format(order_id=order_id)
        resp = httpx.get(self.host + path, headers=self._signed_headers("GET", path), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def cancel_order(self, order_id: str) -> dict:
        """LIVE — pull a resting order off the book. DELETE /portfolio/orders/{id}."""
        import httpx

        path = self.PREFIX + self.ORDER_STATUS_PATH.format(order_id=order_id)
        resp = httpx.delete(self.host + path, headers=self._signed_headers("DELETE", path), timeout=15)
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Run tests**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_execution.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/execution.py tests/test_execution.py
git commit -m "feat(execution): resting GTC orders + get_order/cancel_order"
```

---

### Task 8: Floor maker-order lifecycle (`floor.py`)

**Files:**
- Modify: `src/council/trading/floor.py` (replace `_auto_execute` with `_place_maker`; add `_poll_orders`; wire into `tick` and `_council_eval`)
- Test: `tests/test_floor_council.py` (extend)

**Interfaces:**
- Consumes: `maker_price_cents` (Task 2), `journal.log(status="working", blind_p=...)` / `mark_filled` / `cancel` (Task 6), `trader.place_order(tif="gtc")` / `get_order` / `cancel_order` (Task 7), `kalshi_fee` (fills pay maker rate → use `maker_fee` from Task 1).
- Produces: `FloorState.open_orders: list[dict]` — each `{"order_id": str, "journal_id": int, "key": "council", "tk": str, "side": str, "contracts": int, "px": float, "tick": int, "paper": bool}`. `_place_maker(m, d: Deliberation, dec: Decision, paper: bool) -> bool` (True = order posted; `d` is passed explicitly — never read from `self.last_debate`, which is `None` when called directly), `_poll_orders() -> None` (fills/cancels; runs every tick). `ORDER_TTL_TICKS` env knob. Task 9 wires paper mode through the same lifecycle.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_floor_council.py
import time as _time
from council.trading.floor import FloorState
from council.trading.market import Market


class FakeTrader:
    """Scriptable broker: orders rest, then fill or languish as told."""
    def __init__(self):
        self.placed, self.cancelled = [], []
        self.fills = {}          # order_id -> fill_count to report

    def place_order(self, ticker, side, count, cents, action="buy", tif="ioc"):
        oid = f"o{len(self.placed)}"
        self.placed.append({"id": oid, "ticker": ticker, "side": side,
                            "count": count, "cents": cents, "tif": tif})
        return {"order": {"order_id": oid}}

    def get_order(self, order_id):
        n = self.fills.get(order_id, 0)
        return {"order": {"order_id": order_id, "fill_count": n,
                          "average_fill_price": 0.79 if n else 0.0}}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"order": {"order_id": order_id}}


def _floor_with_fake_trader():
    fs = FloorState()
    fs.live = fs.execute = True
    fs.trader = FakeTrader()
    return fs


def _market():
    return Market("KXM-1", "t", 0.80, volume=500,
                  close_ts=int(_time.time() + 48 * 3600), yes_bid=0.78, yes_ask=0.82)


def _delib(p=0.90):
    from council.trading.deliberation import Deliberation, ModelEstimate
    est = [ModelEstimate("A", p, "t")]
    return Deliberation("KXM-1", est, est, p, 0.01)


def test_place_maker_posts_resting_order_and_journals_working():
    fs = _floor_with_fake_trader()
    from council.trading.deliberation import Decision
    dec = Decision(True, "yes", 5, 79, "test")
    assert fs._place_maker(_market(), _delib(), dec, paper=False)
    assert fs.trader.placed[0]["tif"] == "gtc" and fs.trader.placed[0]["cents"] == 79
    assert len(fs.open_orders) == 1
    row = fs.journal.get(fs.open_orders[0]["journal_id"])
    assert row["status"] == "working"


def test_poll_orders_books_fill():
    fs = _floor_with_fake_trader()
    from council.trading.deliberation import Decision
    fs._place_maker(_market(), _delib(), Decision(True, "yes", 5, 79, "t"), paper=False)
    oid = fs.open_orders[0]["order_id"]
    fs.trader.fills[oid] = 5
    fs._poll_orders()
    assert not fs.open_orders
    assert fs.trades_today == 1
    row = fs.journal._c.execute("SELECT status, fill_price FROM trades").fetchone()
    assert row["status"] == "placed" and abs(row["fill_price"] - 0.79) < 1e-9


def test_poll_orders_cancels_after_ttl():
    fs = _floor_with_fake_trader()
    fs.ORDER_TTL_TICKS = 3
    from council.trading.deliberation import Decision
    fs._place_maker(_market(), _delib(), Decision(True, "yes", 5, 79, "t"), paper=False)
    fs._tick_n += 4
    fs._poll_orders()
    assert fs.trader.cancelled and not fs.open_orders
    assert fs.journal._c.execute("SELECT status FROM trades").fetchone()["status"] == "cancelled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_floor_council.py -k maker -v`
Expected: FAIL — `AttributeError: 'FloorState' object has no attribute '_place_maker'`

- [ ] **Step 3: Write the implementation**

In `src/council/trading/floor.py`:

`__init__` additions (near the exit-params block):

```python
        self.open_orders: list[dict] = []      # resting maker orders awaiting fill/TTL
        self.ORDER_TTL_TICKS = int(os.environ.get("ORDER_TTL_TICKS", 30))
```

Import `maker_fee` alongside `kalshi_fee`: `from .ledger import kalshi_fee, maker_fee`.

Replace `_auto_execute` with `_place_maker` (delete `_auto_execute`; the pending `self._last_fill` plumbing goes with it):

```python
    def _place_maker(self, m, d, dec, paper: bool) -> bool:
        """Post a resting order INSIDE the spread (maker: 75% fee discount, earn the
        spread). Booked only when a poll sees a real fill. Returns True iff posted."""
        entry = dec.limit_price_cents / 100.0
        cost = dec.contracts * entry
        exposure = sum(p["contracts"] * p["entry"] for b in self.books.values() for p in b["open"]) \
            + sum(o["contracts"] * o["px"] for o in self.open_orders)
        daily_pnl = self.journal.realized_today() if self.journal else self.output
        ok, reason = self.guard.check(cost, exposure, daily_pnl)
        if not ok:
            self._log(f"RISK BLOCKED — Council {dec.side.upper()} {m.id}: {reason}")
            return False
        if paper:
            order_id = f"paper-{next(self._ids)}"
        else:
            try:
                resp = self.trader.place_order(m.id, dec.side, dec.contracts,
                                               dec.limit_price_cents, tif="gtc")
            except Exception as exc:  # noqa: BLE001 — never let a broker error crash the loop
                self._log(f"ORDER FAILED — Council {m.id}: {type(exc).__name__}")
                return False
            order_id = str(((resp or {}).get("order") or {}).get("order_id") or "")
            if not order_id:
                self._log(f"ORDER REJECTED — Council {m.id}: no order_id in response")
                return False
        tid = self._journal_log(m, d, dec, side=dec.side, fill_price=0.0,
                                contracts=dec.contracts, edge=0.0, status="working")
        self.open_orders.append({"order_id": order_id, "journal_id": tid, "key": "council",
                                 "tk": m.id, "side": dec.side, "contracts": dec.contracts,
                                 "px": entry, "tick": self._tick_n, "paper": paper})
        self._log(f"{'PAPER ' if paper else ''}MAKER POSTED — Council {dec.side.upper()} "
                  f"{dec.contracts} {m.id} @ {dec.limit_price_cents}¢ (rests, TTL {self.ORDER_TTL_TICKS} ticks)")
        return True

    def _poll_orders(self) -> None:
        """Each tick: book any filled resting order; cancel any past its TTL."""
        if not self.open_orders:
            return
        md = self._md() if any(o["paper"] for o in self.open_orders) else None
        for o in self.open_orders[:]:
            filled, fill_px = 0, o["px"]
            if o["paper"]:
                m = None
                try:
                    m = md.get_market(o["tk"]) if md else None
                except Exception:  # noqa: BLE001
                    pass
                if m is not None:
                    # A resting bid fills when the far side crosses down to it.
                    if o["side"] == "yes" and m.yes_ask and m.yes_ask <= o["px"] + 1e-9:
                        filled = o["contracts"]
                    if o["side"] == "no" and m.yes_bid and (1 - m.yes_bid) <= o["px"] + 1e-9:
                        filled = o["contracts"]
            else:
                try:
                    resp = self.trader.get_order(o["order_id"])
                except Exception:  # noqa: BLE001 — poll again next tick
                    continue
                st = (resp or {}).get("order") or {}
                filled = int(float(st.get("fill_count") or 0))
                if filled:
                    fill_px_yes = float(st.get("average_fill_price") or o["px"])
                    fill_px = fill_px_yes if o["side"] == "yes" else round(1 - fill_px_yes, 4)
            if filled > 0:
                fee = maker_fee(fill_px, filled)
                self.books[o["key"]]["open"].append(
                    {"tk": o["tk"], "contracts": filled, "entry": fill_px, "fee": fee,
                     "ttl": 9_999 if not o["paper"] else random.randint(2, 5)})
                self.journal.mark_filled(o["journal_id"], fill_px, fee, filled)
                self._mirror(o["journal_id"])
                self.trades_today += 1
                self.open_orders.remove(o)
                self._log(f"{'PAPER ' if o['paper'] else ''}MAKER FILLED — Council "
                          f"{o['side'].upper()} {filled} {o['tk']} @ {round(fill_px*100)}¢")
            elif self._tick_n - o["tick"] >= self.ORDER_TTL_TICKS:
                if not o["paper"]:
                    try:
                        self.trader.cancel_order(o["order_id"])
                    except Exception:  # noqa: BLE001 — dropping tracking is still safe: journal row cancels
                        pass
                self.journal.cancel(o["journal_id"])
                self.open_orders.remove(o)
                self._log(f"MAKER EXPIRED — {o['side'].upper()} {o['tk']} unfilled after "
                          f"{self.ORDER_TTL_TICKS} ticks, cancelled")
```

`_journal_log` — return the trade id and accept a status-driven fill shape (change signature line and the end):

```python
    def _journal_log(self, m, d, dec, *, side, fill_price, contracts, edge, status) -> int | None:
        if not self.journal:
            return None
        import statistics as _st
        blind = round(_st.median([e.p_yes for e in d.round1]), 4) if d.round1 else None
        rationale = " | ".join(f"{t.model} {t.p_yes:.2f}" for t in d.round2)
        tid = self.journal.log(market_id=m.id, title=m.title, side=side, converged_p=d.converged_p,
                               blind_p=blind, spread=d.spread, market_price=m.yes_price,
                               executable_price=dec.limit_price_cents / 100.0, edge=edge,
                               contracts=contracts, fill_price=fill_price,
                               fee=kalshi_fee(fill_price, contracts) if contracts and fill_price else 0.0,
                               fill_count=contracts, decision_reason=dec.reason,
                               rationale=rationale, status=status)
        self._mirror(tid)
        return tid
```

`_council_eval` — replace the execute/live branches after `entry = dec.limit_price_cents / 100.0` with the unified maker path:

```python
        if not dec.place:
            self._journal_log(m, d, dec, side=dec.side or "n/a", fill_price=0.0,
                              contracts=0, edge=0.0, status="skipped")
            return
        if self.frozen:
            return
        if self.execute:
            self._place_maker(m, d, dec, paper=False)
        elif self.live:
            self._place_maker(m, d, dec, paper=True)
        else:
            self._council_ticket(m, d, dec)
```

`tick` — add the poll right after the deliberation line:

```python
        if self.live or self.execute:
            self._poll_orders()            # fills/TTL for resting maker orders
```

`_open_market_ids` — predictions must resolve too:

```python
        return [r["market_id"] for r in self.journal._c.execute(
            "SELECT DISTINCT market_id FROM trades WHERE status IN ('placed','skipped','working')").fetchall()]
```

- [ ] **Step 4: Run tests**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_floor_council.py -v`
Expected: new tests PASS. Existing tests exercising `_auto_execute` will FAIL — rewrite them against `_place_maker`/`_poll_orders` (the FakeTrader above replaces the old instant-fill fake). Then full suite: `pytest -q` green.

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/floor.py tests/test_floor_council.py
git commit -m "feat(floor): resting maker-order lifecycle (post -> poll -> fill/TTL-cancel)"
```

---

### Task 9: Wire env knobs, call accounting, and paper mode end-to-end (`floor.py`)

**Files:**
- Modify: `src/council/trading/floor.py` (`enable_live`, `_council_eval` call accounting)
- Test: `tests/test_floor_council.py` (extend)

**Interfaces:**
- Consumes: `DeliberativeCouncil(specs, client, alpha)` (Task 4).
- Produces: `EXTREMIZE_ALPHA` env knob passed to the council; debate call accounting `1 + 2 * len(specs)`; paper mode (live on, execute off) flows through `_place_maker(paper=True)` — no more instant fake fills.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_floor_council.py
def test_enable_live_passes_alpha(monkeypatch):
    monkeypatch.setenv("EXTREMIZE_ALPHA", "1.7")
    fs = FloorState()
    fs._load_live_markets = lambda: []     # no creds needed
    fs.enable_live(budget=1.0)
    assert fs.council.alpha == 1.7


def test_live_call_accounting_counts_two_rounds():
    fs = FloorState()
    fs.live = True
    fs._live_markets = [_market()]
    fs.scout.pick = lambda markets, journal: [markets[0]]   # MockScout.pick is random — pin it
    calls_before = fs.calls
    fs._council_eval()
    # 1 scout + 1 research + 2 rounds x 3 models = counted as 1 + (1 + 6)
    assert fs.calls - calls_before == 1 + 1 + 2 * len(fs.council.specs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_floor_council.py -k "alpha or accounting" -v`
Expected: FAIL — `TypeError: DeliberativeCouncil.__init__ ... 'alpha'` not passed / count is `1 + len(specs)`

- [ ] **Step 3: Write the implementation**

In `enable_live` (floor.py:115), pass alpha:

```python
        self.council = DeliberativeCouncil(
            specs, client, alpha=float(os.environ.get("EXTREMIZE_ALPHA", 1.3)))
```

In `_council_eval`, update the accounting line:

```python
        if self.live:
            self.calls += 1 + 2 * len(self.council.specs)  # 1 research + 2 rounds per model
```

(the MockCouncil test path: give `MockCouncil` an `alpha = 1.0` class attribute so the first test also passes when scout mocks intervene — add `self.alpha = 1.0` to `MockCouncil.__init__`.)

- [ ] **Step 4: Run full suite**

Run: `cd /home/debarshi/council && .venv/bin/pytest -q`
Expected: all green (fix any drift from removed instant paper fills — the paper leaderboard now populates via `_poll_orders`, so tests asserting immediate `books["council"]["open"]` after `_council_eval` must call `fs._poll_orders()` with a crossing quote first).

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/floor.py tests/test_floor_council.py
git commit -m "feat(floor): alpha knob, two-round call accounting, unified paper maker path"
```

---

### Task 10: Calibration arming gate + vault mirror (`floor.py`, `journal_mirror.py`)

**Files:**
- Modify: `src/council/trading/floor.py:137-155` (`arm_execution`), `src/council/trading/floor.py` (`resolve_settled`), `src/council/trading/journal_mirror.py` (add `mirror_calibration`)
- Test: `tests/test_floor_council.py`, `tests/test_obsidian.py` (extend)

**Interfaces:**
- Consumes: `journal.calibration_report(min_n)` (Task 6).
- Produces: `arm_execution` raises `RuntimeError` unless the report shows `beats_market` or `COUNCIL_CALIBRATION_OVERRIDE=true`; `mirror_calibration(vault_dir: str, report: dict) -> None` writes/overwrites `<vault>/wiki/sources/Council Calibration Report.md`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_floor_council.py
import pytest


def test_arm_execution_locked_until_calibrated(monkeypatch):
    monkeypatch.setenv("COUNCIL_MODE", "live")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/k.pem")
    monkeypatch.delenv("COUNCIL_CALIBRATION_OVERRIDE", raising=False)
    fs = FloorState()
    with pytest.raises(RuntimeError, match="calibration gate"):
        fs.arm_execution()


def test_arm_execution_override(monkeypatch):
    monkeypatch.setenv("COUNCIL_MODE", "live")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "kid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/k.pem")
    monkeypatch.setenv("COUNCIL_CALIBRATION_OVERRIDE", "true")
    fs = FloorState()
    fs.enable_live = lambda: None          # don't build a real client
    fs.live = True
    fs.arm_execution()                     # must not raise
    assert fs.execute
```

```python
# append to tests/test_obsidian.py
def test_mirror_calibration_writes_page(tmp_path):
    from council.trading.journal_mirror import mirror_calibration
    rep = {"n": 60, "brier_model": 0.18, "brier_market": 0.21,
           "brier_blind": 0.24, "beats_market": True}
    mirror_calibration(str(tmp_path), rep)
    page = tmp_path / "wiki" / "sources" / "Council Calibration Report.md"
    text = page.read_text()
    assert "0.18" in text and "0.21" in text and "BEATS the market" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/debarshi/council && .venv/bin/pytest tests/test_floor_council.py -k arm tests/test_obsidian.py -k calibration -v`
Expected: FAIL (`arm_execution` arms without any gate; `mirror_calibration` doesn't exist)

- [ ] **Step 3: Write the implementation**

In `arm_execution` (floor.py), after the Kalshi-credentials check and before `if not self.live:`, add:

```python
        min_n = int(os.environ.get("CALIBRATION_MIN_N", 50))
        rep = self.journal.calibration_report(min_n=min_n) if self.journal else {"beats_market": False, "n": 0}
        if not rep["beats_market"]:
            if os.environ.get("COUNCIL_CALIBRATION_OVERRIDE", "").lower() != "true":
                raise RuntimeError(
                    f"calibration gate: {rep['n']}/{min_n} resolved predictions, model Brier "
                    f"{rep.get('brier_model')} vs market {rep.get('brier_market')} — edge not proven. "
                    f"Keep paper-trading, or set COUNCIL_CALIBRATION_OVERRIDE=true to gamble anyway.")
            self._log("⚠ CALIBRATION OVERRIDE — arming without proven edge (explicitly requested).")
```

In `resolve_settled`, after the loop (inside the method), mirror the updated report:

```python
        if self.vault_dir and self.journal:
            from .journal_mirror import mirror_calibration
            try:
                mirror_calibration(self.vault_dir, self.journal.calibration_report(
                    min_n=int(os.environ.get("CALIBRATION_MIN_N", 50))))
            except Exception:  # noqa: BLE001 — vault I/O must never crash the loop
                pass
```

Append to `src/council/trading/journal_mirror.py`:

```python
def mirror_calibration(vault_dir: str, report: dict) -> None:
    """Overwrite the single calibration page — the honest scoreboard, in the vault."""
    import datetime
    from pathlib import Path

    page = Path(vault_dir) / "wiki" / "sources" / "Council Calibration Report.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    verdict = ("BEATS the market — live arming unlocked." if report.get("beats_market")
               else "does NOT beat the market — stay on paper.")
    page.write_text(
        f"---\ntype: source\ntitle: \"Council Calibration Report\"\n"
        f"updated: {datetime.date.today().isoformat()}\ntags:\n  - trading\n  - calibration\n---\n\n"
        f"# Council Calibration Report\n\n"
        f"Resolved predictions: **{report['n']}**\n\n"
        f"| Estimator | Brier (lower = better) |\n|---|---|\n"
        f"| Council (converged) | {report.get('brier_model')} |\n"
        f"| Market price baseline | {report.get('brier_market')} |\n"
        f"| Council (blind round 1) | {report.get('brier_blind')} |\n\n"
        f"**Verdict:** the council currently {verdict}\n",
        encoding="utf-8")
```

- [ ] **Step 4: Run full suite**

Run: `cd /home/debarshi/council && .venv/bin/pytest -q`
Expected: all green. Note: `tests/test_safety.py` and `tests/test_api.py` may construct/arm floors — if any test arms without history, give it the override env (that *is* the new intended behavior).

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/floor.py src/council/trading/journal_mirror.py tests/
git commit -m "feat(gate): Brier calibration lock on arm_execution + vault calibration mirror"
```

---

### Task 11: Demo-host verification script (pre-live gate, no unit tests)

**Files:**
- Create: `scripts/verify_maker_demo.py`

**Interfaces:**
- Consumes: `KalshiTrader` with `KALSHI_HOST=https://external-api.demo.kalshi.co`.
- Produces: a manual pre-live checklist script (same role as `scripts/verify_sell_demo.py`). Not run in CI; run by the user before any armed session.

- [ ] **Step 1: Write the script**

```python
"""Pre-live gate: verify resting (GTC) order place/status/cancel on the Kalshi DEMO host.

Usage: KALSHI_HOST=https://external-api.demo.kalshi.co \
       KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=... \
       .venv/bin/python scripts/verify_maker_demo.py TICKER

Places a 1-contract resting YES bid far below the market (should NOT fill),
polls status, cancels it, and confirms the cancel. Nothing here should cost money.
"""
import os
import sys
import time

from council.trading.execution import KalshiTrader


def main() -> int:
    ticker = sys.argv[1]
    t = KalshiTrader(os.environ["KALSHI_API_KEY_ID"], os.environ["KALSHI_PRIVATE_KEY_PATH"],
                     host=os.environ.get("KALSHI_HOST"))
    resp = t.place_order(ticker, "yes", 1, 2, tif="gtc")     # 2c deep bid — rests
    oid = resp["order"]["order_id"]
    print("placed resting order:", oid)
    time.sleep(2)
    st = t.get_order(oid)
    print("status:", st["order"].get("status"), "fill_count:", st["order"].get("fill_count"))
    assert int(float(st["order"].get("fill_count") or 0)) == 0, "deep bid should not fill"
    print("cancel:", t.cancel_order(oid))
    st = t.get_order(oid)
    print("post-cancel status:", st["order"].get("status"))
    print("OK — resting lifecycle verified on", t.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Sanity-check it imports**

Run: `cd /home/debarshi/council && .venv/bin/python -c "import scripts.verify_maker_demo" 2>/dev/null || .venv/bin/python -m py_compile scripts/verify_maker_demo.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/verify_maker_demo.py
git commit -m "chore(scripts): demo-host resting-order verification (pre-live gate)"
```

---

## Final verification

- [ ] `cd /home/debarshi/council && .venv/bin/pytest -q` — full suite green, no API keys in env.
- [ ] `rg -n "EDGE_THRESHOLD" src/ tests/` — no remaining references.
- [ ] Manual smoke: `council serve`, floor runs in mock mode, snapshot shows `round1` in the debate panel, no exceptions in logs for 50 ticks.
- [ ] Update `.env.example` and `README.md` Safety section: document the new knobs (`EDGE_HORIZON_DAYS`, `EXTREMIZE_ALPHA`, `SPREAD_BUFFER`, `MIN_PROFIT`, `ORDER_TTL_TICKS`, `CALIBRATION_MIN_N`, `COUNCIL_CALIBRATION_OVERRIDE`) and the calibration gate; remove `EDGE_THRESHOLD`. Commit as `docs: document blend-don't-beat knobs + calibration gate`.
