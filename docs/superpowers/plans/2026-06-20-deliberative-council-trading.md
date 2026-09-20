# Deliberative Council Trading — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three independent single-model books with one council that debates each market over two rounds, converges on a probability, and autonomously places or skips a real Kalshi order — with the floor UI showing the live debate.

**Architecture:** A new `DeliberativeCouncil` runs round-1 independent estimates then round-2 revisions (each model sees the others), aggregating to a mean + spread. A pure `decide()` turns that into a place/skip Decision on a deterministic consensus rule. `floor._council_eval` wires scan → research → debate → decide → the existing `_auto_execute` seam. The UI gains a Debate Theater rendered from a new `snapshot.debate` field, animated with anime.js.

**Tech Stack:** Python 3.14, direct LiteLLM via OpenRouter (no CrewAI), FastAPI floor server, vanilla JS canvas + anime.js (CDN). Tests with pytest using fakes (no network, no API keys).

## Global Constraints

- Python 3.14; **no CrewAI** (won't install on 3.14) — live model calls go through the existing `ModelClient` (LiteLLM).
- All new tests run **offline with no API keys**, matching the existing ~67-test suite. Network/model calls are injected as fakes.
- Reuse the existing real-money seam unchanged: `floor._auto_execute` → `RiskGuard.check` → `KalshiTrader.place_order`. Do not modify `execution.py`.
- Risk caps are authoritative and unchanged: `MAX_POSITION_USD=5`, `MAX_TOTAL_USD=50`, `MAX_DAILY_LOSS_USD=20`, plus kill switch.
- Decision thresholds (defaults): `EDGE_THRESHOLD=0.06`, `SPREAD_CAP=0.05` (population stdev of round-2 P(YES)).
- Kalshi limit price must be an integer in `[1, 99]` cents (enforced by `KalshiTrader.build_order`).
- Reuse `analysts.parse_estimate(text, market) -> Estimate` for defensive model-output parsing; `Estimate` has `.prob_yes` and `.thesis`.
- `ModelClient.complete(spec, messages) -> tuple[str, dict]` (text, meta). `ModelSpec(model, goal, temperature=0.2)`.
- UI: gate all anime.js behind `prefers-reduced-motion: no-preference`; keep dark-OLED palette and existing fonts (IBM Plex Mono + Bricolage Grotesque); no emoji icons.

---

### Task 1: Deliberation data models + pure `decide()`

**Files:**
- Create: `src/council/trading/deliberation.py`
- Test: `tests/test_deliberation.py`

**Interfaces:**
- Consumes: `Market` (`.id`, `.title`, `.yes_price`), `RiskGuard` (`.max_position_usd`).
- Produces:
  - `@dataclass ModelEstimate{ model: str, p_yes: float, thesis: str }`
  - `@dataclass Deliberation{ market_id: str, round1: list[ModelEstimate], round2: list[ModelEstimate], converged_p: float, spread: float, notes: str }`
  - `@dataclass Decision{ place: bool, side: str | None, contracts: int, limit_price_cents: int, reason: str }`
  - `decide(d: Deliberation, market: Market, caps: RiskGuard, edge_threshold: float = 0.06, spread_cap: float = 0.05) -> Decision`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deliberation.py
from council.trading.deliberation import Deliberation, ModelEstimate, Decision, decide
from council.trading.execution import RiskGuard
from council.trading.market import Market

CAPS = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)

def _delib(market_id, converged_p, spread):
    return Deliberation(market_id, [], [], converged_p, spread, "")

def test_consensus_place_no_side():
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = _delib(m.id, 0.533, 0.018)          # mean below price -> NO; tight spread
    out = decide(d, m, CAPS)
    assert out.place is True
    assert out.side == "no"
    # NO entry = 1 - 0.62 = 0.38 -> 38c; contracts = floor(5/0.38)=13
    assert out.limit_price_cents == 38
    assert out.contracts == 13

def test_consensus_place_yes_side():
    m = Market("CPI-NOV-HOT", "CPI hot?", 0.41)
    d = _delib(m.id, 0.52, 0.02)            # mean above price -> YES, 11c edge
    out = decide(d, m, CAPS)
    assert out.place is True and out.side == "yes"
    assert out.limit_price_cents == 41

def test_high_spread_skips():
    m = Market("X", "x?", 0.50)
    d = _delib(m.id, 0.70, 0.12)            # big edge but no consensus
    out = decide(d, m, CAPS)
    assert out.place is False and "consensus" in out.reason.lower()

def test_subthreshold_edge_skips():
    m = Market("X", "x?", 0.50)
    d = _delib(m.id, 0.53, 0.01)            # 3c edge < 6c
    out = decide(d, m, CAPS)
    assert out.place is False and "threshold" in out.reason.lower()

def test_price_clamped_to_valid_range():
    m = Market("X", "x?", 0.95)
    d = _delib(m.id, 0.99, 0.0)             # YES, entry 0.95 -> 95c valid
    out = decide(d, m, CAPS)
    assert 1 <= out.limit_price_cents <= 99
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_deliberation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'council.trading.deliberation'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/council/trading/deliberation.py
"""Deliberative council: a two-round debate over one market + a pure decision rule.

The decision rule is deterministic — no model places a trade. The council produces a
converged probability and a disagreement (spread); `decide` gates the order on a fee-aware
edge AND consensus, and sizes within the position cap.
"""
from __future__ import annotations

from dataclasses import dataclass

from .execution import RiskGuard
from .market import Market


@dataclass
class ModelEstimate:
    model: str
    p_yes: float
    thesis: str


@dataclass
class Deliberation:
    market_id: str
    round1: list[ModelEstimate]
    round2: list[ModelEstimate]
    converged_p: float
    spread: float
    notes: str = ""


@dataclass
class Decision:
    place: bool
    side: str | None
    contracts: int
    limit_price_cents: int
    reason: str


def decide(d: Deliberation, market: Market, caps: RiskGuard,
           edge_threshold: float = 0.06, spread_cap: float = 0.05) -> Decision:
    edge = d.converged_p - market.yes_price
    side = "yes" if edge > 0 else "no"
    entry = market.yes_price if side == "yes" else round(1 - market.yes_price, 2)
    if d.spread > spread_cap:
        return Decision(False, None, 0, 0,
                        f"no consensus (spread {d.spread:.3f} > {spread_cap:.3f})")
    if abs(edge) < edge_threshold:
        return Decision(False, None, 0, 0,
                        f"edge {abs(edge)*100:.1f}c < {edge_threshold*100:.0f}c threshold")
    price_cents = min(99, max(1, int(round(entry * 100))))
    contracts = max(1, int(caps.max_position_usd / max(entry, 0.05)))
    return Decision(True, side, contracts, price_cents,
                    f"{abs(edge)*100:.1f}c {side.upper()} edge, spread {d.spread:.3f} ok -> PLACE")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_deliberation.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/deliberation.py tests/test_deliberation.py
git commit -m "feat(trading): deliberation data models + pure consensus decision rule"
```

---

### Task 2: `DeliberativeCouncil.debate()` — two-round debate

**Files:**
- Modify: `src/council/trading/deliberation.py`
- Test: `tests/test_deliberation.py` (add cases)

**Interfaces:**
- Consumes: `ModelClient.complete(spec, messages) -> (text, meta)`, `ModelSpec(model, goal, temperature)`, `analysts.parse_estimate`.
- Produces: `class DeliberativeCouncil.__init__(self, specs: list[ModelSpec], client: ModelClient)` and `debate(self, market: Market, notes: str) -> Deliberation`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_deliberation.py
from council.trading.deliberation import DeliberativeCouncil
from council.models import ModelSpec

class RecordingClient:
    """Returns scripted JSON per model; records messages so we can assert round-2 context."""
    def __init__(self, r1: dict[str, float], r2: dict[str, float]):
        self.r1, self.r2 = r1, r2
        self.messages = []
    def complete(self, spec, messages):
        self.messages.append(messages)
        user = messages[-1]["content"]
        table = self.r2 if "PEER ESTIMATES" in user else self.r1
        p = table[spec.model]
        return f'{{"prob_yes": {p}, "thesis": "model {spec.model} says {p}"}}', {}

def _council():
    specs = [ModelSpec("m-claude", "g"), ModelSpec("m-kimi", "g"), ModelSpec("m-glm", "g")]
    return specs

def test_debate_runs_two_rounds_and_converges():
    specs = _council()
    client = RecordingClient(
        r1={"m-claude": 0.58, "m-kimi": 0.49, "m-glm": 0.55},
        r2={"m-claude": 0.54, "m-kimi": 0.52, "m-glm": 0.54},
    )
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = DeliberativeCouncil(specs, client).debate(m, "Jobs report hot.")
    assert [e.model for e in d.round1] == ["m-claude", "m-kimi", "m-glm"]
    assert len(d.round2) == 3
    assert abs(d.converged_p - (0.54+0.52+0.54)/3) < 1e-6
    assert d.spread > 0
    # 6 model calls total (3 per round)
    assert len(client.messages) == 6

def test_round2_prompt_includes_peer_estimates():
    specs = _council()
    client = RecordingClient(r1={"m-claude":0.5,"m-kimi":0.5,"m-glm":0.5},
                             r2={"m-claude":0.5,"m-kimi":0.5,"m-glm":0.5})
    m = Market("X", "x?", 0.5)
    DeliberativeCouncil(specs, client).debate(m, "notes")
    round2_msgs = [msg for msg in client.messages if "PEER ESTIMATES" in msg[-1]["content"]]
    assert len(round2_msgs) == 3
    # a peer's round-1 number appears in the round-2 prompt
    assert "0.5" in round2_msgs[0][-1]["content"]

def test_unparseable_reply_defers_to_market_price():
    specs = [ModelSpec("m1", "g")]
    class Junk:
        def complete(self, spec, messages): return "no json here", {}
    m = Market("X", "x?", 0.37)
    d = DeliberativeCouncil(specs, Junk()).debate(m, "")
    assert d.round1[0].p_yes == 0.37   # parse_estimate defers to market price
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_deliberation.py -k debate -v`
Expected: FAIL with `ImportError: cannot import name 'DeliberativeCouncil'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/council/trading/deliberation.py
import statistics

from ..models import ModelClient, ModelSpec
from .analysts import parse_estimate

_SYS = ('You are a calibrated prediction-market analyst. '
        'Respond ONLY with JSON: {"prob_yes": <0..1>, "thesis": "<one sentence>"}.')


def _market_block(market: Market, notes: str) -> str:
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"Current YES price: {market.yes_price:.2f}\n"
            f"Research notes:\n{notes}\n")


class DeliberativeCouncil:
    def __init__(self, specs: list[ModelSpec], client: ModelClient) -> None:
        self.specs = specs
        self.client = client

    def _ask(self, spec: ModelSpec, market: Market, user: str) -> ModelEstimate:
        text, _ = self.client.complete(
            spec, [{"role": "system", "content": _SYS}, {"role": "user", "content": user}])
        est = parse_estimate(text, market)
        return ModelEstimate(spec.model, est.prob_yes, est.thesis)

    def debate(self, market: Market, notes: str) -> Deliberation:
        block = _market_block(market, notes)
        round1 = [self._ask(s, market, block + "\nGive your calibrated P(YES) and a one-sentence thesis.")
                  for s in self.specs]
        peer = "PEER ESTIMATES (round 1):\n" + "\n".join(
            f"- {e.model}: P(YES) {e.p_yes:.2f} — {e.thesis}" for e in round1)
        round2 = [self._ask(s, market, block + "\n" + peer +
                            "\nReconsider in light of your peers and give your final P(YES) and one-sentence thesis.")
                  for s in self.specs]
        ps = [e.p_yes for e in round2]
        converged = sum(ps) / len(ps)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, round1, round2, round(converged, 4), round(spread, 4), notes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_deliberation.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/deliberation.py tests/test_deliberation.py
git commit -m "feat(trading): two-round DeliberativeCouncil debate"
```

---

### Task 3: `WebSearchResearch` provider

**Files:**
- Create: `src/council/trading/research.py`
- Test: `tests/test_research.py`

**Interfaces:**
- Consumes: `Market` (`.title`, `.id`), `ModelClient`, `ModelSpec`.
- Produces:
  - `class WebSearchResearch.__init__(self, search_fn: Callable[[str], str])` with `context_for(self, market: Market) -> str` (satisfies `analysts.ResearchProvider`).
  - `def openrouter_online_search(client: ModelClient, model_slug: str = "openrouter/openai/gpt-4o-mini:online") -> Callable[[str], str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_research.py
from council.trading.research import WebSearchResearch
from council.trading.market import Market

def test_returns_search_notes():
    r = WebSearchResearch(lambda q: f"- 2026-06-19: news about {q}")
    assert "news about Fed cuts?" in r.context_for(Market("FED", "Fed cuts?", 0.6))

def test_blank_search_falls_back():
    r = WebSearchResearch(lambda q: "   ")
    assert r.context_for(Market("X", "x?", 0.5)) == "No external signal available."

def test_search_error_falls_back_gracefully():
    def boom(q): raise RuntimeError("network down")
    r = WebSearchResearch(boom)
    assert r.context_for(Market("X", "x?", 0.5)) == "No external signal available."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_research.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'council.trading.research'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/council/trading/research.py
"""Research feed: turn a market into a few dated, sourced snippets for the debate.

`WebSearchResearch` wraps an injected `search_fn` so tests pass a fake and no network is
touched. `openrouter_online_search` is the live backend (OpenRouter ':online' web plugin) —
one call per market, shared across all debaters. Failures degrade to the no-signal string;
they never raise into the trading loop.
"""
from __future__ import annotations

from typing import Callable

from ..models import ModelClient, ModelSpec
from .market import Market

_NONE = "No external signal available."


class WebSearchResearch:
    def __init__(self, search_fn: Callable[[str], str]) -> None:
        self._search = search_fn

    def context_for(self, market: Market) -> str:
        try:
            notes = self._search(market.title or market.id)
        except Exception:  # noqa: BLE001 — never let research crash the loop
            return _NONE
        return notes.strip() or _NONE


def openrouter_online_search(client: ModelClient,
                             model_slug: str = "openrouter/openai/gpt-4o-mini:online") -> Callable[[str], str]:
    def _search(query: str) -> str:
        spec = ModelSpec(model_slug, "Gather decision-relevant market research.")
        text, _ = client.complete(spec, [
            {"role": "system", "content":
                "You are a research assistant with web access. Reply with 3-5 short, dated, "
                "sourced bullet points relevant to the prediction market. No preamble."},
            {"role": "user", "content":
                f"Find recent, decision-relevant facts for this prediction market: {query}"},
        ])
        return text
    return _search
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_research.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/research.py tests/test_research.py
git commit -m "feat(trading): web-search research provider (injectable backend)"
```

---

### Task 4: Wire the council into the floor (`_council_eval`)

**Files:**
- Modify: `src/council/trading/floor.py`
- Test: `tests/test_floor_council.py`

**Interfaces:**
- Consumes: `DeliberativeCouncil`, `decide`, `Decision`, `WebSearchResearch`, existing `KalshiTrader`, `RiskGuard`, `_auto_execute`.
- Produces on `FloorState`: attributes `council`, `research`, `last_debate: Deliberation | None`, `trades_today: int`, `MAX_TRADES_PER_DAY`, `DELIBERATE_EVERY`; methods `cheap_score(market) -> float`, `_council_eval()`.

**Notes for the implementer:** `FloorState.__init__` already sets `self.live`, `self.execute`, `self.trader`, `self.guard`, `self.calls`, `self.markets`, `self.books`, `self._live_markets`, `self.output`. `enable_live()` currently builds per-book `_analysts`; replace that body. `_auto_execute(self, key, m, side, entry, contracts)` already exists and calls `RiskGuard.check` then `KalshiTrader.place_order` — call it with `key="council"`. Add a `"council"` pseudo-book to `self.books` in `__init__` (copy an existing book dict shape: keys `book,name,key,skill,slug,strat,pnl,open,wins,trades`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_floor_council.py
from council.trading.floor import FloorState
from council.trading.deliberation import Deliberation, ModelEstimate
from council.trading.market import Market
from council.trading.execution import RiskGuard

class FakeCouncil:
    def __init__(self, p, spread): self.p, self.spread = p, spread
    def debate(self, market, notes):
        e = [ModelEstimate("a", self.p, "t"), ModelEstimate("b", self.p, "t")]
        return Deliberation(market.id, e, e, self.p, self.spread, notes)

class FakeResearch:
    def context_for(self, market): return "notes"

class FakeTrader:
    def __init__(self): self.orders = []
    def place_order(self, ticker, side, count, cents): self.orders.append((ticker, side, count, cents))

def _armed_floor(council):
    f = FloorState()
    f.live = True
    f.execute = True
    f.council = council
    f.research = FakeResearch()
    f.trader = FakeTrader()
    f.guard = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)
    f._live_markets = [Market("FED-DEC-CUT", "Fed cuts?", 0.62)]
    return f

def test_council_eval_places_real_order_on_consensus():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))   # 12c NO edge, tight
    f._council_eval()
    assert f.trader.orders == [("FED-DEC-CUT", "no", 13, 38)]
    assert f.last_debate is not None
    assert f.trades_today == 1

def test_council_eval_skips_on_no_consensus():
    f = _armed_floor(FakeCouncil(p=0.20, spread=0.20))   # huge edge but no consensus
    f._council_eval()
    assert f.trader.orders == []
    assert f.trades_today == 0

def test_daily_trade_cap_halts():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.MAX_TRADES_PER_DAY = 1
    f._council_eval(); f._council_eval()
    assert len(f.trader.orders) == 1

def test_snapshot_exposes_debate():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f._council_eval()
    snap = f.snapshot()
    assert snap["debate"]["market_id"] == "FED-DEC-CUT"
    assert len(snap["debate"]["round2"]) == 2
    assert snap["debate"]["decision"]["place"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_floor_council.py -v`
Expected: FAIL with `AttributeError: 'FloorState' object has no attribute '_council_eval'`

- [ ] **Step 3: Write minimal implementation**

In `src/council/trading/floor.py`:

1. Add imports near the top:
```python
from .deliberation import DeliberativeCouncil, decide, Decision
from .research import WebSearchResearch, openrouter_online_search
from .analysts import MockResearch
```

2. In `FloorState.__init__`, after `self._live_markets: list = []`, add:
```python
        self.council = None
        self.research = None
        self.last_debate = None
        self.trades_today = 0
        self.DELIBERATE_EVERY = int(os.environ.get("DELIBERATE_EVERY", 5))
        self.MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 10))
        self._council_seen: set[str] = set()
        self.books["council"] = {"book": "★", "name": "Council", "key": "council",
                                 "skill": 0.5, "slug": "council", "strat": "consensus",
                                 "pnl": 0.0, "open": [], "wins": 0, "trades": 0}
```

3. Replace the body of `enable_live` with:
```python
    def enable_live(self, budget: float = 10.0) -> None:
        client = ModelClient(budget_usd=budget)
        specs = [ModelSpec(bk["slug"], "Estimate a calibrated P(YES).")
                 for k, bk in self.books.items() if k != "council"]
        self.council = DeliberativeCouncil(specs, client)
        if os.environ.get("COUNCIL_RESEARCH", "online").lower() == "online":
            self.research = WebSearchResearch(openrouter_online_search(client))
        else:
            self.research = MockResearch()
        self._live_markets = self._load_live_markets()
        self.live = True
        self._log(f"LIVE armed — council active over {len(self._live_markets)} markets. Tokens will be spent.")
```

4. Add the ranking + eval methods (place after `_load_live_markets`):
```python
    def cheap_score(self, m) -> float:
        """Rank without an LLM: prefer liquid markets priced away from the extremes."""
        return m.volume * (1.0 - abs(0.5 - m.yes_price) * 2)

    def _council_eval(self) -> None:
        if self.trades_today >= self.MAX_TRADES_PER_DAY:
            return
        pool = [m for m in self._live_markets if m.id not in self._council_seen] or self._live_markets
        if not pool:
            return
        m = max(pool, key=self.cheap_score)
        self._council_seen.add(m.id)
        notes = self.research.context_for(m) if self.research else "No external signal available."
        d = self.council.debate(m, notes)
        dec = decide(d, m, self.guard)
        self.last_debate = (d, dec)
        self.calls += 1
        self._log(f"Council debate {m.id}: P(YES) {d.converged_p:.2f} vs {m.yes_price:.2f} "
                  f"(spread {d.spread:.3f}) — {dec.reason}")
        if dec.place and self.execute and not self.frozen:
            entry = dec.limit_price_cents / 100.0
            self._auto_execute("council", m, dec.side, entry, dec.contracts)
            self.trades_today += 1
```

5. In `tick()`, replace the live branch `if self._tick_n % self.LIVE_EVERY == 0: self._live_eval()` with:
```python
            if self._tick_n % self.DELIBERATE_EVERY == 0:
                self._council_eval()
```
   (Delete the now-unused `_live_eval` and `_analysts` references; keep `_mock_gen` for the offline demo path.)

6. In `snapshot()`, add a `debate` key to the returned dict:
```python
            "debate": self._debate_snapshot(),
```
   and add the helper:
```python
    def _debate_snapshot(self):
        if not self.last_debate:
            return None
        d, dec = self.last_debate
        return {
            "market_id": d.market_id,
            "converged_p": d.converged_p, "spread": d.spread, "notes": d.notes,
            "round1": [{"model": e.model, "p": e.p_yes, "thesis": e.thesis} for e in d.round1],
            "round2": [{"model": e.model, "p": e.p_yes, "thesis": e.thesis} for e in d.round2],
            "decision": {"place": dec.place, "side": dec.side, "contracts": dec.contracts,
                         "cents": dec.limit_price_cents, "reason": dec.reason},
        }
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (existing ~67 + new tests green; no test references `_live_eval`/per-book `_analysts`)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/floor.py tests/test_floor_council.py
git commit -m "feat(trading): council debate drives the floor (scan->debate->decide->execute)"
```

---

### Task 5: Floor UI — Debate Theater + anime.js

**Files:**
- Modify: `web/floor-game.html`
- Verify: `node --check` on the extracted script + live `curl`

**Interfaces:**
- Consumes: `GET /api/floor` snapshot field `debate` (shape from Task 4 `_debate_snapshot`).
- Produces: a Debate Theater panel + animated beats; no server changes.

**Notes for the implementer:** the page already polls `/api/floor` in `poll()` and renders the activity feed (added 2026-06-20). Add `renderDebate(d.debate)` to `poll()`. Load anime.js via CDN in `<head>`: `<script src="https://cdn.jsdelivr.net/npm/animejs@3.2.2/lib/anime.min.js"></script>`.

- [ ] **Step 1: Add the anime.js CDN tag** in `<head>` (after the existing font `<link>` lines).

- [ ] **Step 2: Add the Debate Theater markup** after the `<div class="dock" id="dock"></div>` block:

```html
  <section class="theater" id="theater" hidden aria-live="polite">
    <h4><span class="dotpulse"></span> COUNCIL DEBATE — <span id="th-market">—</span></h4>
    <div class="cols" id="th-cols"></div>
    <div class="verdict" id="th-verdict"></div>
  </section>
```

- [ ] **Step 3: Add CSS** (in the `<style>` block, near `.feed`):

```css
  .theater{margin-top:12px;border:1px solid var(--line);border-radius:11px;background:var(--panel);padding:12px 13px}
  .theater[hidden]{display:none}
  .theater h4{margin:0 0 10px;font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.1em;color:var(--muted);display:flex;align-items:center;gap:8px}
  .theater .cols{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
  .col{border:1px solid var(--line);border-radius:9px;padding:10px;background:#0d121b}
  .col .nm{font-family:var(--disp);font-weight:700;font-size:13px}
  .col .p{font-family:var(--mono);font-size:20px;margin:4px 0;font-variant-numeric:tabular-nums}
  .col .p .r1{color:var(--faint);font-size:12px}
  .col .th{font-size:11.5px;color:var(--muted);line-height:1.45}
  .verdict{margin-top:10px;font-family:var(--mono);font-size:12.5px;padding:8px 11px;border-radius:8px;text-align:center}
  .verdict.place{color:var(--up);background:rgba(61,220,132,.1);border:1px solid var(--up)}
  .verdict.skip{color:var(--faint);background:#0d121b;border:1px solid var(--line)}
```

- [ ] **Step 4: Add the render function** and call it in `poll()` (after `renderFeed(...)`):

```javascript
function renderDebate(dz){
  const sec=document.getElementById("theater");
  if(!dz){ sec.hidden=true; return; }
  const reduce=window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.getElementById("th-market").textContent=dz.market_id;
  const r1=Object.fromEntries(dz.round1.map(e=>[e.model,e]));
  document.getElementById("th-cols").innerHTML=dz.round2.map(e=>`
    <div class="col" data-model="${e.model}">
      <div class="nm">${e.model}</div>
      <div class="p"><span class="r2">${(e.p*100).toFixed(0)}%</span>
        <span class="r1">r1 ${((r1[e.model]?.p ?? e.p)*100).toFixed(0)}%</span></div>
      <div class="th">${e.thesis}</div>
    </div>`).join("");
  const v=document.getElementById("th-verdict"), dec=dz.decision;
  v.className="verdict "+(dec.place?"place":"skip");
  v.textContent=dec.place
    ? `PLACE ${dec.side.toUpperCase()} ${dec.contracts} @ ${dec.cents}¢ · ${dec.reason}`
    : `SKIP · ${dec.reason}`;
  const wasHidden=sec.hidden; sec.hidden=false;
  if(!reduce && wasHidden && window.anime){
    anime({targets:"#th-cols .col",opacity:[0,1],translateY:[8,0],delay:anime.stagger(80),duration:260,easing:"easeOutQuad"});
    anime({targets:"#th-verdict",opacity:[0,1],scale:[0.96,1],duration:240,easing:"easeOutBack"});
  }
}
```

In `poll()`, add after the existing `renderFeed(d.activity||[]);` line:
```javascript
    renderDebate(d.debate);
```

- [ ] **Step 5: Verify (no browser test framework — syntax + serve check)**

Run:
```bash
.venv/bin/python -c "import re,pathlib; h=pathlib.Path('web/floor-game.html').read_text(); s=re.search(r'<script>\n(.*?)</script>',h,re.S).group(1); pathlib.Path('/tmp/floor.js').write_text(s)"
node --check /tmp/floor.js && echo "JS OK"
curl -s "http://127.0.0.1:8777/" | grep -c "renderDebate"
```
Expected: `JS OK`, and grep count ≥ 2 (server serves the updated file live).

- [ ] **Step 6: Commit**

```bash
git add web/floor-game.html
git commit -m "feat(ui): live Council Debate Theater on the floor (anime.js)"
```

---

## Self-Review

**Spec coverage:**
- Debate & converge → Task 2. Deterministic consensus rule → Task 1. Research feed → Task 3. Replace A/B/C + scan-and-pick + daily cap → Task 4. UI debate view + anime.js → Task 5. Real-money seam reuse → Task 4 (`_auto_execute`, no `execution.py` change). All covered.
- Config knobs (`EDGE_THRESHOLD`, `SPREAD_CAP`, `MAX_TRADES_PER_DAY`, `DELIBERATE_EVERY`, `COUNCIL_RESEARCH`) → defaults in Tasks 1 & 4.

**Type consistency:** `Deliberation`/`ModelEstimate`/`Decision` fields defined in Task 1 are used unchanged in Tasks 2 & 4. `decide(d, market, caps, edge_threshold, spread_cap)` signature consistent. `_auto_execute("council", m, side, entry, contracts)` matches the existing floor method signature `(self, key, m, side, entry, contracts)`. `snapshot()["debate"]` shape (Task 4) matches `renderDebate` consumption (Task 5).

**Placeholders:** none — every code step is complete.

**Risk note:** `place_order` remains unverified against a real Kalshi order (carried from Phase 1); caps + kill switch + daily cap are the protection. First real order is still the live test.

## Out of scope (later)

Dedicated search-API backend; multi-market parallel debates; threshold tuning from realized P&L; Kalshi demo-host order verification; the suspend-hang fix needed before unattended overnight running.
