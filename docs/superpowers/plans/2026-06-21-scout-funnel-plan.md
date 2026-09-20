# Scout Funnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut wasted frontier-model spend by 10-20x on intervals that end in SKIP by inserting a 3-tier funnel (free structural filter, one cheap Kimi K2.6 call, then optionally the full 3-model council debate) before any expensive roundtable debate fires.

**Architecture:** One new file `src/council/trading/scout.py` containing the `Scout` class (Tier 0 `shortlist`, Tier 1 `pick`), `structural_score()`, and `MockScout`. Integration changes in `floor.py`: read env vars, wire the scout into `_council_eval()` and `enable_live()`, backward-compat fallback when `SCOUT_MODEL=""`. One new test file `tests/test_scout.py`. The existing deliberation, decision, and execution paths are unchanged.

**Tech Stack:** Python 3.14, pytest with fakes (no API keys, no network). `ModelClient` with overridden `_invoke` for the scout's Tier-1 call.

## Global Constraints

- All tests run offline with synthetic `Market` objects and fake `ModelClient` -- no API keys, no network.
- `SCOUT_MODEL` defaults to `openrouter/moonshotai/kimi-k2.6`. Empty string disables the scout (backward-compat fallback).
- `SCOUT_SHORTLIST` defaults to `8`. `SCOUT_MAX_ESCALATE` defaults to `1`.
- Env vars read via `os.environ.get(...)` in `FloorState.__init__`, matching the existing config pattern.
- The scout's Tier-1 call increments `self.calls` by 1 (not by 1+len(specs)).
- `structural_score` starts with equal weighting across its four signal components; weights are tunable later from journal data.
- `pick()` fails closed: any parse error returns `[]` (never escalate on garbage).
- `MockScout` mirrors `MockCouncil`'s pattern: deterministic, no API calls, usable in paper mode.
- Branch off `main`, do not push.

---

### Task 1: `structural_score()` -- Tier-0 pure-Python scoring function

**Files:**
- Create: `src/council/trading/scout.py`
- Create: `tests/test_scout.py`

**Interfaces:**
- Consumes: `Market` dataclass from `council.trading.market` (fields: `yes_price`, `volume`, `close_ts`, `yes_bid`, `yes_ask`).
- Produces: `structural_score(m: Market) -> float` -- module-level pure function. Higher = more interesting. Markets below `FloorState.MIN_VOLUME` (50) are not filtered here (that happens in the pool before shortlisting).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scout.py`:

```python
import time
from council.trading.market import Market


def _m(id="X", price=0.50, volume=1000, close_ts=0, bid=0.0, ask=0.0):
    return Market(id=id, title="t", yes_price=price, volume=volume,
                  close_ts=close_ts, yes_bid=bid, yes_ask=ask)


def test_structural_score_tail_detection():
    """Markets at extreme prices (longshot/near-cert) score higher than mid-priced."""
    from council.trading.scout import structural_score
    mid = structural_score(_m(price=0.50, volume=1000))
    longshot = structural_score(_m(price=0.05, volume=1000))
    near_cert = structural_score(_m(price=0.95, volume=1000))
    assert longshot > mid
    assert near_cert > mid


def test_structural_score_wide_spread():
    """Wide bid/ask spread scores higher than tight spread."""
    from council.trading.scout import structural_score
    tight = structural_score(_m(bid=0.49, ask=0.51))
    wide = structural_score(_m(bid=0.30, ask=0.70))
    assert wide > tight


def test_structural_score_closing_soon():
    """Markets closing in hours score higher than those closing in days."""
    from council.trading.scout import structural_score
    now = int(time.time())
    soon = structural_score(_m(close_ts=now + 3600))       # 1 hour
    later = structural_score(_m(close_ts=now + 86400 * 7)) # 7 days
    assert soon > later


def test_structural_score_stale_price_high_volume():
    """High volume + mid price (not moved) scores higher than low volume."""
    from council.trading.scout import structural_score
    stale_busy = structural_score(_m(price=0.50, volume=50000))
    stale_quiet = structural_score(_m(price=0.50, volume=500))
    assert stale_busy > stale_quiet
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'council.trading.scout'`

- [ ] **Step 3: Implement `structural_score`**

Create `src/council/trading/scout.py`:

```python
"""Scout funnel: cheap pre-screen before the expensive council debate.

Three tiers:
  Tier 0 — structural_score + shortlist (free, pure Python)
  Tier 1 — one cheap LLM call to triage the shortlist (pick)
  Tier 2 — full council debate (existing roundtable, unchanged)
"""
from __future__ import annotations

import time

from .market import Market


def structural_score(m: Market) -> float:
    """Tier-0 composite mispricing signal. Pure, deterministic, no API calls.

    Four equally-weighted components (each normalized to roughly 0-1):
      1. Stale-price-vs-volume: volume / hours-to-close (same idea as cheap_score)
      2. Favorite-longshot tails: distance from 0.5 (extremes are interesting)
      3. Wide bid/ask spread: wider = potentially mispriced
      4. Closing-soon recency: inverse hours to close
    """
    # 1. Volume intensity (volume per hour to close; higher = busier + sooner)
    hrs = max((m.close_ts - time.time()) / 3600.0, 0.25) if m.close_ts else 9999.0
    vol_intensity = m.volume / hrs

    # Normalize to ~0-1 range (10k vol/hr is very high)
    vol_score = min(vol_intensity / 10000.0, 1.0)

    # 2. Tail detection: how far from 0.5 (extremes have behavioral bias)
    tail_score = abs(m.yes_price - 0.5) * 2.0  # 0 at mid, 1.0 at extremes

    # 3. Bid/ask spread width (wider = potentially mispriced or illiquid)
    spread = (m.yes_ask - m.yes_bid) if (m.yes_ask and m.yes_bid) else 0.0
    spread_score = min(max(spread, 0.0) / 0.20, 1.0)  # cap at 20c spread

    # 4. Closing-soon urgency (inverse hours; sooner = more actionable)
    urgency_score = min(1.0 / hrs, 1.0) if hrs < 9999.0 else 0.0

    # Equal weights (tunable later from journal data)
    return vol_score + tail_score + spread_score + urgency_score
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py -v`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/scout.py tests/test_scout.py
git commit -m "feat(scout): add structural_score Tier-0 scoring function with tests"
```

---

### Task 2: `Scout.shortlist()` -- Tier-0 ranked shortlist

**Files:**
- Modify: `src/council/trading/scout.py` (add `Scout.__init__` and `shortlist`)
- Modify: `tests/test_scout.py` (add shortlist tests)

**Interfaces:**
- Consumes: `structural_score(m)` from Task 1; `Market` dataclass; `ModelClient` from `council.models`.
- Produces: `Scout.__init__(self, client: ModelClient, model: str, shortlist_n: int, max_escalate: int)` and `Scout.shortlist(self, markets: list[Market]) -> list[Market]` -- returns up to `shortlist_n` markets, best-first by `structural_score`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scout.py`:

```python
def test_shortlist_returns_top_n():
    """shortlist(n=3) on 5 markets returns 3, best structural_score first."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    now = int(time.time())
    markets = [
        _m(id="A", price=0.50, volume=100, close_ts=now + 86400 * 7),
        _m(id="B", price=0.05, volume=1000, close_ts=now + 3600),       # longshot + closing soon
        _m(id="C", price=0.95, volume=2000, close_ts=now + 7200),       # near-cert + high vol
        _m(id="D", price=0.50, volume=200, close_ts=now + 86400),
        _m(id="E", price=0.50, volume=500, close_ts=now + 86400 * 3),
    ]
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=3, max_escalate=1)
    result = scout.shortlist(markets)
    assert len(result) == 3
    ids = [m.id for m in result]
    # B and C should be in top 3 (tail + urgency); A should NOT be (boring mid-price, far out)
    assert "B" in ids and "C" in ids
    assert "A" not in ids


def test_shortlist_empty_pool():
    """shortlist([]) returns []."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=8, max_escalate=1)
    assert scout.shortlist([]) == []


def test_shortlist_fewer_than_n():
    """When pool < shortlist_n, return all markets."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    markets = [_m(id="A"), _m(id="B")]
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.shortlist(markets)
    assert len(result) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py::test_shortlist_returns_top_n tests/test_scout.py::test_shortlist_empty_pool tests/test_scout.py::test_shortlist_fewer_than_n -v`
Expected: FAIL with `ImportError` (Scout class doesn't exist yet)

- [ ] **Step 3: Implement `Scout.__init__` and `shortlist`**

In `src/council/trading/scout.py`, add below the `structural_score` function:

```python
from ..models import ModelClient, ModelSpec
from .journal import Journal


class Scout:
    """Three-tier funnel: structural filter -> cheap LLM triage -> escalate."""

    def __init__(self, client: ModelClient, model: str, shortlist_n: int, max_escalate: int) -> None:
        self.client = client
        self.model = model
        self.shortlist_n = shortlist_n
        self.max_escalate = max_escalate

    def shortlist(self, markets: list[Market]) -> list[Market]:
        """Tier 0: pure-Python structural ranking. No API calls.
        Returns up to self.shortlist_n markets, best-first."""
        if not markets:
            return []
        ranked = sorted(markets, key=structural_score, reverse=True)
        return ranked[:self.shortlist_n]
```

Note: the imports of `ModelClient`, `ModelSpec`, and `Journal` should go at the top of the file with the other imports. The full import block at the top of `scout.py` should be:

```python
from __future__ import annotations

import json
import re
import time

from ..models import ModelClient, ModelSpec
from .journal import Journal
from .market import Market
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py -v`
Expected: 7 PASSED (4 from Task 1 + 3 new)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/scout.py tests/test_scout.py
git commit -m "feat(scout): add Scout class with shortlist() Tier-0 ranking"
```

---

### Task 3: `Scout.pick()` -- Tier-1 cheap LLM triage with brain injection

**Files:**
- Modify: `src/council/trading/scout.py` (add `pick` method)
- Modify: `tests/test_scout.py` (add pick tests with fake ModelClient)

**Interfaces:**
- Consumes: `Scout.shortlist_n`, `Scout.max_escalate`, `Scout.client`, `Scout.model` from Task 2; `Journal.calibration()` and `Journal.recall(market_id)` from `council.trading.journal`.
- Produces: `Scout.pick(self, markets: list[Market], journal: Journal) -> list[Market]` -- returns 0..max_escalate markets flagged as genuinely mispriced, or `[]`. Makes one `client.complete()` call. Injects journal calibration + per-market recall. Defensive-parses the response.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scout.py`:

```python
from council.models import ModelClient, ModelSpec, Usage
from council.trading.journal import Journal


class _FakeScoutClient(ModelClient):
    """Fake that captures the prompt and returns a canned response."""
    def __init__(self, response: str):
        super().__init__(budget_usd=100.0)
        self.response = response
        self.captured_messages: list[list[dict]] = []

    def _invoke(self, spec, messages):
        self.captured_messages.append(messages)
        return self.response, Usage()


def _journal():
    return Journal(":memory:")


def test_pick_flags_valid_market():
    """pick() returns the matching Market when the scout flags a valid ticker."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": ["MKT-A"]}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    markets = [_m(id="MKT-A"), _m(id="MKT-B")]
    result = scout.pick(markets, _journal())
    assert len(result) == 1
    assert result[0].id == "MKT-A"


def test_pick_returns_empty_on_none():
    """pick() returns [] when the scout says NONE."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": []}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([_m(id="MKT-A")], _journal())
    assert result == []


def test_pick_returns_empty_on_malformed():
    """pick() returns [] (fail closed) on garbage response."""
    from council.trading.scout import Scout
    client = _FakeScoutClient("this is total garbage with no JSON at all")
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([_m(id="MKT-A")], _journal())
    assert result == []


def test_pick_filters_hallucinated_ticker():
    """pick() ignores market IDs not in the input list."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": ["FAKE-TICKER"]}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([_m(id="MKT-A")], _journal())
    assert result == []


def test_pick_caps_at_max_escalate():
    """Even if the scout flags 5 markets, only max_escalate=1 is returned."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": ["A", "B", "C", "D", "E"]}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    markets = [_m(id=x) for x in "ABCDE"]
    result = scout.pick(markets, _journal())
    assert len(result) == 1
    assert result[0].id == "A"


def test_pick_empty_shortlist_no_api_call():
    """pick([]) returns [] without making any API call."""
    from council.trading.scout import Scout
    client = _FakeScoutClient("should not be called")
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([], _journal())
    assert result == []
    assert client.captured_messages == []


def test_pick_injects_journal_calibration_and_recall():
    """The prompt sent to the scout model contains journal calibration stats
    and per-market recall lessons."""
    from council.trading.scout import Scout
    j = _journal()
    # Seed the journal with a resolved trade so calibration + recall are non-empty
    j.log(market_id="MKT-A", title="t", side="no", converged_p=0.34, spread=0.02,
          market_price=0.93, executable_price=0.07, edge=0.10, contracts=3,
          fill_price=0.07, fee=0.01, fill_count=3, decision_reason="r", rationale="x")
    j.resolve("MKT-A", "yes")  # NO lost

    client = _FakeScoutClient('{"escalate": []}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    scout.pick([_m(id="MKT-A")], j)

    # Flatten all message content into one string
    all_text = " ".join(msg["content"] for conv in client.captured_messages for msg in conv)
    # calibration() stats should appear
    assert "overall" in all_text.lower() or "correct" in all_text.lower()
    # recall(market_id) lessons should appear
    assert "MKT" in all_text
    assert "LESSONS" in all_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py::test_pick_flags_valid_market tests/test_scout.py::test_pick_returns_empty_on_none tests/test_scout.py::test_pick_returns_empty_on_malformed tests/test_scout.py::test_pick_filters_hallucinated_ticker tests/test_scout.py::test_pick_caps_at_max_escalate tests/test_scout.py::test_pick_empty_shortlist_no_api_call tests/test_scout.py::test_pick_injects_journal_calibration_and_recall -v`
Expected: FAIL (pick method doesn't exist yet)

- [ ] **Step 3: Implement `Scout.pick()`**

Add the `pick` method to the `Scout` class in `src/council/trading/scout.py`:

```python
    def pick(self, markets: list[Market], journal: Journal) -> list[Market]:
        """Tier 1: one cheap LLM call over the Tier-0 shortlist.
        Returns 0..max_escalate markets flagged as genuinely mispriced, or [].
        Injects journal calibration + per-market recall as context."""
        if not markets:
            return []

        market_by_id = {m.id: m for m in markets}
        prompt = self._build_prompt(markets, journal)
        spec = ModelSpec(self.model, goal="scout")

        try:
            text, _ = self.client.complete(
                spec, [{"role": "system", "content": self._system_prompt()},
                       {"role": "user", "content": prompt}])
        except Exception:
            return []  # fail closed

        return self._parse_response(text, market_by_id)

    def _system_prompt(self) -> str:
        return (
            "You are a prediction-market scout. Your job is to identify markets that "
            "look genuinely mispriced from a shortlist of candidates. You must name a "
            "SPECIFIC CATALYST — what the market is missing — to flag a market. Without "
            "a concrete reason, return NONE.\n\n"
            "Respond with ONLY a JSON object: {\"escalate\": [\"TICKER-1\", ...]} "
            "with the ticker IDs of markets worth escalating to a full debate, or "
            "{\"escalate\": []} if none look promising."
        )

    def _build_prompt(self, markets: list[Market], journal: Journal) -> str:
        cal = journal.calibration()
        lines = ["# Your track record"]
        o = cal["overall"]
        if o["n"] > 0:
            lines.append(f"Overall: {o['win']}/{o['n']} correct ({round(100 * o['win'] / o['n'])}%)")
            for bucket, data in cal["by_edge"].items():
                if data["n"] > 0:
                    lines.append(f"  {bucket} edges: {data['win']}/{data['n']} correct")
        else:
            lines.append("No resolved history yet.")

        lines.append("\n# Candidate markets")
        for m in markets:
            hrs = max((m.close_ts - time.time()) / 3600.0, 0.25) if m.close_ts else None
            hrs_str = f"{hrs:.1f}h to close" if hrs and hrs < 9999 else "unknown close"
            spread_str = f"bid {m.yes_bid:.2f} / ask {m.yes_ask:.2f}" if (m.yes_bid or m.yes_ask) else "no quote"
            rules_summary = (m.rules[:200] + "...") if len(m.rules) > 200 else m.rules
            lessons = journal.recall(m.id)
            lines.append(
                f"\n## {m.id}: {m.title}\n"
                f"YES price: {m.yes_price:.2f} | {spread_str} | vol: {m.volume} | {hrs_str}\n"
                f"Rules: {rules_summary or 'none'}\n"
                f"{lessons}"
            )

        return "\n".join(lines)

    def _parse_response(self, text: str, market_by_id: dict[str, Market]) -> list[Market]:
        """Defensive parse: extract market IDs, validate against input, cap at max_escalate.
        On any failure, return [] (fail closed)."""
        try:
            # Try JSON parse first
            data = json.loads(text)
            ids = data.get("escalate", [])
        except (json.JSONDecodeError, AttributeError):
            # Fallback: try to extract JSON from mixed text
            match = re.search(r'\{[^}]*"escalate"\s*:\s*\[([^\]]*)\][^}]*\}', text)
            if not match:
                return []
            try:
                data = json.loads(match.group(0))
                ids = data.get("escalate", [])
            except (json.JSONDecodeError, AttributeError):
                return []

        if not isinstance(ids, list):
            return []

        result = []
        for ticker in ids:
            if not isinstance(ticker, str):
                continue
            if ticker in market_by_id:
                result.append(market_by_id[ticker])
            if len(result) >= self.max_escalate:
                break
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py -v`
Expected: 14 PASSED (7 from Tasks 1-2 + 7 new)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/scout.py tests/test_scout.py
git commit -m "feat(scout): add Scout.pick() Tier-1 LLM triage with brain injection and defensive parse"
```

---

### Task 4: `MockScout` for paper mode

**Files:**
- Modify: `src/council/trading/scout.py` (add `MockScout` class)
- Modify: `tests/test_scout.py` (add MockScout tests)

**Interfaces:**
- Consumes: `Market` dataclass, `Journal` from prior tasks.
- Produces: `MockScout` class with same interface as `Scout` (`shortlist(markets)`, `pick(markets, journal)`). No API calls. Returns a deterministic random subset (or `[]`) for paper-mode activity.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scout.py`:

```python
def test_mock_scout_shortlist_returns_subset():
    """MockScout.shortlist returns up to shortlist_n markets."""
    from council.trading.scout import MockScout
    markets = [_m(id=x) for x in "ABCDE"]
    mock = MockScout(shortlist_n=3, max_escalate=1)
    result = mock.shortlist(markets)
    assert len(result) <= 3
    assert all(m in markets for m in result)


def test_mock_scout_pick_returns_at_most_max_escalate():
    """MockScout.pick returns 0 or 1 markets (max_escalate=1), no API calls."""
    from council.trading.scout import MockScout
    markets = [_m(id="A"), _m(id="B")]
    mock = MockScout(shortlist_n=8, max_escalate=1)
    # Run multiple times to verify it never exceeds max_escalate
    for _ in range(20):
        result = mock.pick(markets, _journal())
        assert len(result) <= 1
        assert all(m in markets for m in result)


def test_mock_scout_empty_input():
    """MockScout handles empty inputs gracefully."""
    from council.trading.scout import MockScout
    mock = MockScout(shortlist_n=8, max_escalate=1)
    assert mock.shortlist([]) == []
    assert mock.pick([], _journal()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py::test_mock_scout_shortlist_returns_subset tests/test_scout.py::test_mock_scout_pick_returns_at_most_max_escalate tests/test_scout.py::test_mock_scout_empty_input -v`
Expected: FAIL with `ImportError` (MockScout doesn't exist yet)

- [ ] **Step 3: Implement `MockScout`**

Add to the bottom of `src/council/trading/scout.py`:

```python
class MockScout:
    """Offline stand-in for Scout -- produces mock shortlist/pick results with no API
    calls, so the floor's free PAPER mode generates scout activity for the UI."""

    def __init__(self, shortlist_n: int = 8, max_escalate: int = 1) -> None:
        self.shortlist_n = shortlist_n
        self.max_escalate = max_escalate

    def shortlist(self, markets: list[Market]) -> list[Market]:
        """Tier 0: same structural ranking as the real Scout."""
        if not markets:
            return []
        ranked = sorted(markets, key=structural_score, reverse=True)
        return ranked[:self.shortlist_n]

    def pick(self, markets: list[Market], journal: Journal) -> list[Market]:
        """Mock Tier 1: randomly returns 0 or up to max_escalate markets.
        ~50% chance of returning nothing (mimics real scout NONE rate)."""
        import random
        if not markets:
            return []
        if random.random() < 0.5:
            return []
        return [random.choice(markets)][:self.max_escalate]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py -v`
Expected: 17 PASSED (14 from Tasks 1-3 + 3 new)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/scout.py tests/test_scout.py
git commit -m "feat(scout): add MockScout for paper mode (mirrors MockCouncil pattern)"
```

---

### Task 5: Integrate scout into `FloorState.__init__` and `enable_live()`

**Files:**
- Modify: `src/council/trading/floor.py:11,21-22,41-88,90-101` (imports, `__init__`, `enable_live`)
- Modify: `tests/test_scout.py` (add integration tests for wiring)

**Interfaces:**
- Consumes: `Scout`, `MockScout` from `council.trading.scout` (Tasks 1-4); `ModelClient` from `council.models`; env vars `SCOUT_MODEL`, `SCOUT_SHORTLIST`, `SCOUT_MAX_ESCALATE`.
- Produces: `FloorState.scout` attribute (either `MockScout` in paper mode or `Scout` in live mode). `FloorState._scout_model`, `FloorState._scout_shortlist`, `FloorState._scout_max_escalate` config attrs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scout.py`:

```python
def test_floor_init_has_mock_scout(monkeypatch):
    """FloorState() in default paper mode wires a MockScout."""
    monkeypatch.delenv("SCOUT_MODEL", raising=False)
    from council.trading.floor import FloorState
    from council.trading.scout import MockScout
    f = FloorState()
    assert isinstance(f.scout, MockScout)
    assert f.scout.shortlist_n == 8
    assert f.scout.max_escalate == 1


def test_floor_init_custom_env(monkeypatch):
    """FloorState reads SCOUT_SHORTLIST and SCOUT_MAX_ESCALATE from env."""
    monkeypatch.setenv("SCOUT_SHORTLIST", "4")
    monkeypatch.setenv("SCOUT_MAX_ESCALATE", "2")
    from council.trading.floor import FloorState
    f = FloorState()
    assert f.scout.shortlist_n == 4
    assert f.scout.max_escalate == 2


def test_floor_init_scout_disabled(monkeypatch):
    """SCOUT_MODEL="" disables the scout -- floor.scout is None."""
    monkeypatch.setenv("SCOUT_MODEL", "")
    from council.trading.floor import FloorState
    f = FloorState()
    assert f.scout is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py::test_floor_init_has_mock_scout tests/test_scout.py::test_floor_init_custom_env tests/test_scout.py::test_floor_init_scout_disabled -v`
Expected: FAIL with `AttributeError: 'FloorState' object has no attribute 'scout'`

- [ ] **Step 3: Implement the wiring**

In `src/council/trading/floor.py`, add the import near the top (after the existing deliberation import on line 21):

```python
from .scout import Scout, MockScout
```

In `FloorState.__init__` (after the `self.council = MockCouncil(...)` and `self.research = MockResearch()` lines, around line 87), add:

```python
        # Scout funnel: cheap pre-screen before expensive council debate.
        self._scout_model = os.environ.get("SCOUT_MODEL", "openrouter/moonshotai/kimi-k2.6")
        self._scout_shortlist = int(os.environ.get("SCOUT_SHORTLIST", 8))
        self._scout_max_escalate = int(os.environ.get("SCOUT_MAX_ESCALATE", 1))
        if self._scout_model:
            self.scout = MockScout(shortlist_n=self._scout_shortlist,
                                   max_escalate=self._scout_max_escalate)
        else:
            self.scout = None  # scout disabled — fallback to old cheap_score path
```

In `enable_live()` (after `self.council = DeliberativeCouncil(specs, client)` on line 95), add:

```python
        if self._scout_model:
            self.scout = Scout(client=client, model=self._scout_model,
                               shortlist_n=self._scout_shortlist,
                               max_escalate=self._scout_max_escalate)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py -v`
Expected: 20 PASSED (17 + 3 new)

- [ ] **Step 5: Run full suite to check for regressions**

Run: `cd ~/council && .venv/bin/python -m pytest -v`
Expected: 93 PASSED (no regressions -- `_council_eval` not changed yet, scout exists but isn't called)

- [ ] **Step 6: Commit**

```bash
git add src/council/trading/floor.py tests/test_scout.py
git commit -m "feat(scout): wire Scout/MockScout into FloorState init and enable_live"
```

---

### Task 6: Rewrite `_council_eval()` to use the scout funnel

**Files:**
- Modify: `src/council/trading/floor.py:196-252` (`_council_eval` method)
- Modify: `tests/test_scout.py` (add integration tests for the full funnel)

**Interfaces:**
- Consumes: `self.scout` (Scout or MockScout or None) from Task 5; `self.council.debate()`, `decide()`, all existing floor state.
- Produces: Modified `_council_eval()` that runs shortlist->pick->escalate->debate when scout is enabled, or falls back to old `max(pool, key=self.cheap_score)` when `self.scout is None`. `self.calls` increments by 1 for the scout call (live mode only).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scout.py`:

```python
from council.trading.deliberation import Deliberation, ModelEstimate
from council.trading.floor import FloorState
from council.trading.execution import RiskGuard


class _FixedScout:
    """Test scout that returns a fixed result."""
    def __init__(self, escalate_ids: list[str], shortlist_n=8, max_escalate=1):
        self._escalate_ids = set(escalate_ids)
        self.shortlist_n = shortlist_n
        self.max_escalate = max_escalate
        self.shortlist_called = False
        self.pick_called = False

    def shortlist(self, markets):
        self.shortlist_called = True
        return markets[:self.shortlist_n]

    def pick(self, markets, journal):
        self.pick_called = True
        return [m for m in markets if m.id in self._escalate_ids][:self.max_escalate]


class _FakeCouncilForScout:
    specs = [1, 2, 3]
    def __init__(self):
        self.debated = []
    def debate(self, market, notes, lessons=""):
        self.debated.append(market.id)
        e = [ModelEstimate("a", 0.50, "t"), ModelEstimate("b", 0.50, "t")]
        return Deliberation(market.id, e, e, 0.50, 0.01, notes)

class _FakeResearchForScout:
    def context_for(self, market): return "notes"


def _floor_with_scout(scout, council=None, live=False):
    f = FloorState()
    f.scout = scout
    f.council = council or _FakeCouncilForScout()
    f.research = _FakeResearchForScout()
    f.live = live
    if live:
        f._live_markets = [
            _m(id="MKT-A", price=0.62, volume=1000),
            _m(id="MKT-B", price=0.50, volume=2000),
        ]
        f.execute = False  # live but unarmed
    else:
        f.markets = [
            _m(id="MKT-A", price=0.62, volume=1000),
            _m(id="MKT-B", price=0.50, volume=2000),
        ]
    f.guard = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)
    return f


def test_funnel_escalated_debates_flagged_market():
    """When the scout flags MKT-A, council.debate() is called exactly once on MKT-A."""
    council = _FakeCouncilForScout()
    f = _floor_with_scout(_FixedScout(escalate_ids=["MKT-A"]), council=council)
    f._council_eval()
    assert council.debated == ["MKT-A"]


def test_funnel_nothing_escalated_no_debate():
    """When the scout returns [], council.debate() is never called."""
    council = _FakeCouncilForScout()
    f = _floor_with_scout(_FixedScout(escalate_ids=[]), council=council)
    f._council_eval()
    assert council.debated == []
    assert any("no candidates escalated" in a["s"].lower() for a in f.activity)


def test_funnel_call_counter_increments_by_one():
    """In live mode, the scout call increments self.calls by 1."""
    f = _floor_with_scout(_FixedScout(escalate_ids=["MKT-A"]), live=True)
    initial_calls = f.calls
    f._council_eval()
    # scout call = +1, debate call = +1 + len(specs) = +4; total = +5
    # But the scout increment is the NEW behavior; debate increment is existing.
    # We check that the total includes the scout's +1.
    assert f.calls == initial_calls + 1 + 1 + len(f.council.specs)


def test_funnel_backward_compat_scout_disabled(monkeypatch):
    """With scout=None (SCOUT_MODEL=""), the old cheap_score path fires."""
    monkeypatch.setenv("SCOUT_MODEL", "")
    f = FloorState()
    council = _FakeCouncilForScout()
    f.council = council
    f.research = _FakeResearchForScout()
    assert f.scout is None
    f._council_eval()
    # Old path: picks max by cheap_score and debates it
    assert len(council.debated) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py::test_funnel_escalated_debates_flagged_market tests/test_scout.py::test_funnel_nothing_escalated_no_debate tests/test_scout.py::test_funnel_call_counter_increments_by_one tests/test_scout.py::test_funnel_backward_compat_scout_disabled -v`
Expected: FAIL (the current `_council_eval` doesn't use the scout at all)

- [ ] **Step 3: Rewrite `_council_eval()` in `floor.py`**

Replace the body of `_council_eval()` (lines 196-252 of `src/council/trading/floor.py`) with:

```python
    def _council_eval(self) -> None:
        # Token backstop only bounds LIVE (real-spend) mode; free mock runs unbounded.
        if self.live and self.calls >= self.MAX_LIVE_CALLS:
            self.live = False
            self._log(f"live call cap ({self.MAX_LIVE_CALLS}) reached — LIVE auto-disabled")
            return
        today = date.today()
        if today != self._day:
            self._day = today
            self.trades_today = 0
            self._council_seen.clear()
        if self.trades_today >= self.MAX_TRADES_PER_DAY:
            return
        markets = self._live_markets if self.live else self.markets
        pool = [m for m in markets if m.id not in self._council_seen]
        if not pool:                       # debated them all — rotate fresh
            self._council_seen.clear()
            pool = list(markets)
        if not pool:
            return

        # ── scout funnel (new) ─────────────────────────────────────────────
        if self.scout is not None:
            shortlist = self.scout.shortlist(pool)
            escalated = self.scout.pick(shortlist, self.journal) if shortlist else []
            if self.live and shortlist:
                self.calls += 1  # one cheap scout call
            if not escalated:
                self._log("Scout: no candidates escalated — skipping debate")
                return
            m = escalated[0]
        else:
            # Backward compat: SCOUT_MODEL="" disables the scout
            m = max(pool, key=self.cheap_score)

        self._council_seen.add(m.id)
        notes = self.research.context_for(m) if self.research else "No external signal available."
        lessons = self.journal.recall(m.id) if self.journal else ""
        d = self.council.debate(m, notes, lessons)
        dec = decide(d, m, self.guard, edge_threshold=self.EDGE_THRESHOLD, spread_cap=self.SPREAD_CAP)
        self.last_debate = (d, dec)
        if self.live:
            self.calls += 1 + len(self.council.specs)  # 1 research + 1 turn per model (lean roundtable)
        self._log(f"Council debate {m.id}: P(YES) {d.converged_p:.2f} vs {m.yes_price:.2f} "
                  f"(spread {d.spread:.3f}) — {dec.reason}")
        if not dec.place:
            self._journal_log(m, d, dec, side=dec.side or "n/a", fill_price=0.0,
                              contracts=0, edge=0.0, status="skipped")
            return
        entry = dec.limit_price_cents / 100.0
        if self.execute and not self.frozen:
            self._last_fill = None
            if self._auto_execute("council", m, dec.side, entry, dec.contracts):
                self.trades_today += 1
                fill = self._last_fill or {"price": entry, "count": dec.contracts}
                self._journal_log(m, d, dec, side=dec.side, fill_price=fill["price"],
                                  contracts=fill["count"],
                                  edge=abs(d.converged_p - m.yes_price), status="placed")
        elif self.live:
            self.books["council"]["open"].append(
                {"tk": m.id, "contracts": dec.contracts, "entry": entry,
                 "fee": kalshi_fee(entry, dec.contracts), "ttl": random.randint(2, 5)})
            self._journal_log(m, d, dec, side=dec.side, fill_price=entry,
                              contracts=dec.contracts,
                              edge=abs(d.converged_p - m.yes_price), status="placed")
            self._log(f"PAPER FILL — Council {dec.side.upper()} {dec.contracts} {m.id} @ {dec.limit_price_cents}¢")
        else:
            self._council_ticket(m, d, dec)
```

- [ ] **Step 4: Run the new integration tests**

Run: `cd ~/council && .venv/bin/python -m pytest tests/test_scout.py -v`
Expected: 24 PASSED (20 + 4 new)

- [ ] **Step 5: Run the FULL test suite to check for regressions**

Run: `cd ~/council && .venv/bin/python -m pytest -v`
Expected: All existing 93 tests still pass. The existing `test_floor_council.py` and `test_execution.py` tests use `FakeCouncil` directly and bypass the scout (they set `f.council = FakeCouncil(...)` and the floor's `MockScout` will fire but the mock council it calls is theirs). Verify carefully: if any existing test breaks because of the scout intercepting, the fix is to set `f.scout = None` in that test's `_armed_floor` helper so it falls through to the old path. Note this in the commit message if needed.

**Important regression check:** The existing `_armed_floor` helper in `test_floor_council.py` and `test_execution.py` sets `f.live = True` and `f._live_markets = [...]` with specific markets. The new `_council_eval` will now run `self.scout.shortlist(pool)` then `self.scout.pick(shortlist, journal)` first. Since `FloorState()` initializes `self.scout = MockScout(...)` by default, the MockScout's random `pick()` may return `[]` (50% chance), causing `_council_eval` to skip the debate entirely. This WILL break the existing tests intermittently. The fix: in those test helpers, set `f.scout = None` to use the backward-compat path, keeping those tests deterministic and unchanged.

In `tests/test_floor_council.py`, in the `_armed_floor` function (line 25-33), add after `f.guard = ...`:

```python
    f.scout = None  # disable scout funnel — these tests exercise the council directly
```

In `tests/test_execution.py`, in the `_armed_floor` function (line 68-78), add after `f.guard = guard`:

```python
    f.scout = None  # disable scout funnel — these tests exercise execution directly
```

- [ ] **Step 6: Re-run the full suite after regression fixes**

Run: `cd ~/council && .venv/bin/python -m pytest -v`
Expected: 93 + new scout tests = 97+ PASSED, 0 FAILED

- [ ] **Step 7: Commit**

```bash
git add src/council/trading/floor.py tests/test_scout.py tests/test_floor_council.py tests/test_execution.py
git commit -m "feat(scout): rewrite _council_eval to use scout funnel with backward-compat fallback

Shortlist -> pick -> escalate -> debate. When SCOUT_MODEL is empty string,
falls back to the old cheap_score single-market path. Existing integration
tests set scout=None to stay deterministic."
```

---

### Task 7: Final verification -- full suite green, branch hygiene

**Files:**
- No new files. Read-only verification.

**Interfaces:**
- Consumes: Everything from Tasks 1-6.
- Produces: Confidence that the feature is complete, all tests pass, and the branch is ready for review.

- [ ] **Step 1: Run the complete test suite**

Run: `cd ~/council && .venv/bin/python -m pytest -v --tb=short`
Expected: 117 tests PASSED (93 original + 24 new: 4 structural_score + 3 shortlist + 7 pick + 3 MockScout + 3 floor-init + 4 integration). All must pass, 0 failures.

- [ ] **Step 2: Verify no untracked files leaked**

Run: `cd ~/council && git status`
Expected: Clean working tree on the feature branch. Only `src/council/trading/scout.py`, `tests/test_scout.py`, `src/council/trading/floor.py`, `tests/test_floor_council.py`, and `tests/test_execution.py` should show as modified/added.

- [ ] **Step 3: Verify the scout module is importable**

Run: `cd ~/council && .venv/bin/python -c "from council.trading.scout import Scout, MockScout, structural_score; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Verify backward compat (scout disabled)**

Run: `cd ~/council && SCOUT_MODEL="" .venv/bin/python -c "from council.trading.floor import FloorState; f = FloorState(); assert f.scout is None; print('backward compat OK')"`
Expected: `backward compat OK`

- [ ] **Step 5: Note -- this is a local feature branch off main, do NOT push**

This branch should be reviewed locally before pushing. The plan is complete.
