# Smarter Council Decisions (Phase A) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the council decide better — feed it the resolution rules and live quote, judge edges against the executable price, and treat the market price as a strong prior — so the three live-run failure modes (bad fills, misread "edges", betting blind vs efficient markets) stop at the source.

**Architecture:** Three localized changes to `market.py` (carry `rules`) and `deliberation.py` (executable-edge `decide()`, richer debate prompt). No new files. Backward-compatible: when a market has no live quote, `decide()` falls back to the mid, so existing behavior/tests are unchanged.

**Tech Stack:** Python 3.14, pytest with fakes (no keys/network).

## Global Constraints
- All tests run offline with no API keys (inject fakes).
- `decide()` defaults stay `edge_threshold=0.06, spread_cap=0.05, full_conviction_edge=0.20` (the floor passes its own daytrade values at call time; tests call with defaults).
- Mid-fallback must preserve current numbers: when `yes_bid`/`yes_ask` are 0, `decide()` must produce the same side/contracts/price as today.
- Kalshi limit price stays an integer in `[1,99]` cents.

---

### Task 1: Carry resolution rules on `Market`

**Files:**
- Modify: `src/council/trading/market.py` (`Market.rules` field; `_to_market` parses it)
- Test: `tests/test_trading.py`

**Interfaces:**
- Produces: `Market.rules: str` (default `""`), populated from Kalshi `rules_primary` + `rules_secondary`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_trading.py`)

```python
def test_to_market_parses_resolution_rules():
    from council.trading.market import KalshiMarketData
    m = KalshiMarketData._to_market({
        "ticker": "X", "title": "t", "yes_bid_dollars": "0.58", "yes_ask_dollars": "0.60",
        "volume_fp": "100", "rules_primary": "Resolves YES if core PCE is above 0.2%.",
        "rules_secondary": "Per the BEA monthly release.",
    })
    assert "above 0.2%" in m.rules and "BEA" in m.rules
    assert KalshiMarketData._to_market({"ticker": "Y", "title": "t"}).rules == ""
```

- [ ] **Step 2: Run it, expect fail**

Run: `.venv/bin/pytest tests/test_trading.py::test_to_market_parses_resolution_rules -v`
Expected: FAIL (`AttributeError: 'Market' object has no attribute 'rules'` or assertion error)

- [ ] **Step 3: Implement**

In `src/council/trading/market.py`, add a field to the `Market` dataclass after `yes_ask`:
```python
    rules: str = ""           # resolution criteria (how the contract settles)
```
In `KalshiMarketData._to_market`, add to the `Market(...)` construction:
```python
            rules=(str(m.get("rules_primary", "") or "")
                   + (" " + str(m.get("rules_secondary", "")) if m.get("rules_secondary") else "")).strip()[:600],
```

- [ ] **Step 4: Run it, expect pass**

Run: `.venv/bin/pytest tests/test_trading.py::test_to_market_parses_resolution_rules -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/market.py tests/test_trading.py
git commit -m "feat(market): carry Kalshi resolution rules on Market"
```

---

### Task 2: Execution-aware `decide()`

**Files:**
- Modify: `src/council/trading/deliberation.py` (`decide()`)
- Test: `tests/test_deliberation.py`

**Interfaces:**
- Consumes: `Deliberation.converged_p`, `Deliberation.spread`; `Market.yes_bid`, `Market.yes_ask`, `Market.yes_price`; `RiskGuard.max_position_usd`.
- Produces: same `Decision` shape; edge now measured against the executable price.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_deliberation.py`)

```python
def test_decide_wide_spread_erases_edge_skips():
    # mid looks like a 5c YES edge, but the ask is 0.70 -> no real edge -> SKIP (the T30 fix)
    m = Market("X", "x?", 0.50, yes_bid=0.30, yes_ask=0.70)
    out = decide(_delib(m.id, 0.55, 0.0), m, CAPS)
    assert out.place is False

def test_decide_tight_spread_places_executable():
    m = Market("X", "x?", 0.50, yes_bid=0.49, yes_ask=0.51)
    out = decide(_delib(m.id, 0.62, 0.0), m, CAPS)   # 0.62 - 0.51 ask = 11c YES edge
    assert out.place is True and out.side == "yes"

def test_decide_falls_back_to_mid_without_quote():
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)     # no bid/ask -> mid fallback
    out = decide(_delib(m.id, 0.533, 0.018), m, CAPS)
    assert out.place is True and out.side == "no" and out.limit_price_cents == 38
```

- [ ] **Step 2: Run, expect fail** (the wide-spread one places today)

Run: `.venv/bin/pytest tests/test_deliberation.py -k decide_ -v`
Expected: `test_decide_wide_spread_erases_edge_skips` FAILS (currently places)

- [ ] **Step 3: Implement** — replace the body of `decide()` in `deliberation.py` with:

```python
def decide(d: Deliberation, market: Market, caps: RiskGuard,
           edge_threshold: float = 0.06, spread_cap: float = 0.05,
           full_conviction_edge: float = 0.20) -> Decision:
    # Executable prices: pay the ask to buy YES, hit the bid to sell (buy NO). Fall back to
    # the mid when there's no live quote (keeps legacy behavior + tests stable).
    ask = market.yes_ask or market.yes_price
    bid = market.yes_bid or market.yes_price
    yes_edge = d.converged_p - ask        # buy YES profit per contract
    no_edge = bid - d.converged_p         # buy NO == sell YES at the bid
    if yes_edge >= no_edge:
        side, edge, entry = "yes", yes_edge, ask
    else:
        side, edge, entry = "no", no_edge, round(1 - bid, 2)
    price_cents = min(99, max(1, int(round(entry * 100))))
    if d.spread > spread_cap:
        return Decision(False, None, 0, price_cents,
                        f"no consensus (spread {d.spread:.3f} > {spread_cap:.3f})")
    if edge < edge_threshold:
        return Decision(False, None, 0, price_cents,
                        f"exec edge {edge*100:.1f}c < {edge_threshold*100:.0f}c vs live quote -> SKIP")
    # Conviction-scaled sizing on the EXECUTABLE edge (within RiskGuard caps).
    span = max(full_conviction_edge - edge_threshold, 1e-9)
    conviction = min(1.0, (edge - edge_threshold) / span)
    agreement = 1.0 - min(d.spread / spread_cap, 1.0)
    size_frac = 0.3 + 0.7 * conviction * agreement
    contracts = max(1, int((caps.max_position_usd * size_frac) / max(entry, 0.05)))
    return Decision(True, side, contracts, price_cents,
                    f"{edge*100:.1f}c {side.upper()} exec-edge, spread {d.spread:.3f}, "
                    f"size {size_frac*100:.0f}% -> PLACE")
```

- [ ] **Step 4: Run the full deliberation + floor suites**

Run: `.venv/bin/pytest tests/test_deliberation.py tests/test_floor_council.py -q`
Expected: PASS. (Existing decide/floor tests use markets without a quote → mid fallback → unchanged numbers. If any existing assertion shifted, the implementer must STOP and report — mid-fallback was supposed to be exact; do not weaken assertions to force green.)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/deliberation.py tests/test_deliberation.py
git commit -m "fix(trading): decide() judges edge vs executable price (kills wide-spread bad fills)"
```

---

### Task 3: Debate prompt — rules, live quote, market-as-prior

**Files:**
- Modify: `src/council/trading/deliberation.py` (`_market_block`, `_ROUNDTABLE_SYS`)
- Test: `tests/test_deliberation.py`

**Interfaces:**
- Consumes: `Market.rules`, `Market.yes_bid`, `Market.yes_ask`.
- Produces: every debater's prompt now contains the resolution rules, the live quote, and a market-as-prior instruction.

- [ ] **Step 1: Write the failing test** (append to `tests/test_deliberation.py`)

```python
def test_debate_prompt_has_rules_quote_and_market_prior():
    client = RoundtableClient({s.model: 0.5 for s in _PANEL})
    m = Market("X", "x?", 0.50, yes_bid=0.48, yes_ask=0.52, rules="Resolves YES if above 60.")
    DeliberativeCouncil(_PANEL, client).debate(m, "some notes")
    allmsgs = " ".join(msg["content"] for conv in client.messages for msg in conv)
    assert "above 60" in allmsgs                              # resolution rules surfaced
    assert "0.48" in allmsgs and "0.52" in allmsgs            # live quote surfaced
    assert "prior" in allmsgs.lower() and "catalyst" in allmsgs.lower()  # market-as-prior
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_deliberation.py::test_debate_prompt_has_rules_quote_and_market_prior -v`
Expected: FAIL (rules/quote/prior not in prompt)

- [ ] **Step 3: Implement** in `deliberation.py`:

Replace `_market_block`:
```python
def _market_block(market: Market, notes: str) -> str:
    quote = (f"Live quote: YES bid {market.yes_bid:.2f} / ask {market.yes_ask:.2f}\n"
             if (market.yes_ask or market.yes_bid) else "")
    rules = f"Resolution rules: {market.rules}\n" if market.rules else ""
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"Current YES price: {market.yes_price:.2f}\n"
            f"{quote}{rules}Research notes:\n{notes}\n")
```

Replace `_ROUNDTABLE_SYS` with (append the market-as-prior + rules guidance):
```python
_ROUNDTABLE_SYS = (
    "You are {name}, one of three sharp prediction-market analysts at a roundtable with "
    "Claude, GPT-5.4, and Kimi K2. You are pricing ONE market together. The CURRENT MARKET "
    "PRICE is a STRONG PRIOR — it already reflects the crowd and informed traders. Only "
    "deviate materially from it if you can name a SPECIFIC CATALYST the market is missing; "
    "absent a concrete reason, converge toward the price. Read the resolution rules carefully "
    "(misreading the threshold or direction is the most common, costly error). Read the "
    "research and what your colleagues have said, engage directly (agree, push back, refine), "
    "keep it to 2-3 sentences, and END with a line exactly:\nP(YES): <number between 0 and 1>"
)
```

- [ ] **Step 4: Run, expect pass + full suite**

Run: `.venv/bin/pytest tests/test_deliberation.py -q && .venv/bin/pytest -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/council/trading/deliberation.py tests/test_deliberation.py
git commit -m "feat(trading): debate sees resolution rules + live quote, treats price as a strong prior"
```

---

## Self-Review

**Spec coverage:** Change 1 (resolution rules) → Tasks 1 & 3. Change 2 (execution-aware) → Task 2 + quote in Task 3. Change 3 (market-as-prior) → Task 3. All three covered.

**Placeholders:** none — every step has complete code.

**Type consistency:** `Market.rules` (Task 1) consumed in Task 3. `decide()` signature/`Decision` shape unchanged (Task 2). `RoundtableClient`/`_PANEL`/`_delib`/`CAPS` already exist in `tests/test_deliberation.py`. `Market(..., yes_bid=, yes_ask=, rules=)` kwargs exist after Task 1 + the prior fills-fix fields.

**Backward-compat:** Task 2's mid-fallback keeps existing quote-less tests numerically identical; Step 4 explicitly guards against silently weakening them.

## Out of scope
Phase B (trade journal, mark-to-market, resolver, vault mirror, lessons-recall) — separate spec/plan.
