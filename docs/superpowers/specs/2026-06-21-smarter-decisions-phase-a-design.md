# Phase A — Smarter Council Decisions (design)

**Date:** 2026-06-21
**Status:** approved design, pending spec review
**Repo:** `~/council`, branch `feat/deliberative-council`
**Predecessor:** the first live armed run (2026-06-21) exposed three decision-quality failures.
**Successor:** Phase B — the learning loop / "brain" (separate spec).

## Goal
Make the council *decide better* so bad trades are never proposed — fixing the three failure
modes from the live run at their source, not with blunt output filters.

## The three failures it fixes
1. **Bad fills on wide spreads** — `KXHORMUZWEEKLY-T30`: bought NO at 95¢ (worth 5¢) because
   the edge was judged vs the *mid* but execution crossed a wide spread.
2. **Confidently-wrong "edges" vs efficient markets** — `T50`: council 0.34 vs market 0.93,
   almost certainly a contract misread.
3. **Treating the market price as an opponent** rather than as information.

## Locked decisions (from brainstorming)
- Build Phase A first (no new infra); Phase B (journal/learning) is a separate cycle.
- Reasoning improvements, not wide restrictions.

## Change 1 — Resolution rules in the debate
**Why:** the council only sees title + price + web research, not the actual settlement
criteria, so it misreads thresholds/direction (the root of the fake big edges).
- `Market` gains `rules: str = ""`. `KalshiMarketData._to_market` populates it from
  `rules_primary` (+ `rules_secondary` if present), truncated to ~600 chars.
- `_market_block` (deliberation.py) includes a `Resolution rules:` section so every debater
  sees exactly how the contract settles.

## Change 2 — Execution-aware pricing
**Why:** decisions must reflect the price actually paid, not the mid; this makes wide-spread
bad fills self-eliminate (no spread cutoff needed).
- The debate prompt shows the live quote: `YES bid X / ask Y (spread Z)`.
- `decide()` evaluates the edge against the **executable** price using the bid/ask already on
  `Market` (added in the fills fix):
  - buy-YES edge = `converged_p − yes_ask`  (you pay the ask)
  - buy-NO  edge = `yes_bid − converged_p`  (you sell YES at the bid; NO profit = yes_bid − p)
  - choose the side whose executable edge ≥ `edge_threshold`; if neither clears, SKIP.
  - when bid/ask are unknown (0), fall back to the current mid-based edge.
  - conviction-scaled sizing uses this **executable** edge (not the mid edge), so a thin
    real edge sizes small even if the mid looked attractive.
- A wide spread shrinks both executable edges → naturally no trade. The reason string names
  the executable price (e.g. "buy-NO edge 1.2¢ vs ask — SKIP").

## Change 3 — Market price as a strong prior (name-the-catalyst)
**Why:** stop the council from blindly betting against an efficient aggregate.
- Restructure the roundtable system prompt: the market price is a **strong prior** that
  already contains the crowd and informed traders. Each model must explicitly address:
  *"What might the market know that I don't? What specific, nameable catalyst makes it wrong
  here?"* — and only deviate materially from the price if it can name one. Absent a concrete
  reason, converge toward the market.
- No new turns (keeps the lean roundtable); this is prompt content + the closer (Kimi) is
  instructed to discount large divergences that lack a catalyst.

## Files
- **Modify:** `src/council/trading/market.py` (`Market.rules`; `_to_market` parses
  `rules_primary`/`rules_secondary`).
- **Modify:** `src/council/trading/deliberation.py` (`_market_block` adds rules + quote;
  `decide()` executable-edge logic; roundtable system prompt = market-as-prior).
- **Modify:** `src/council/trading/floor.py` — none required for logic (the `Market` passed to
  `debate()`/`decide()` already carries rules + bid/ask); verify the reason string still logs.
- **Tests:** `tests/test_deliberation.py` (decide + prompt) and `tests/test_trading.py` (the
  `_to_market` rules parsing, alongside the existing market tests).

## Testing (offline, no keys — matches the 75-test suite)
- `decide()` executable-edge table:
  - tight spread, real edge → PLACE correct side;
  - **wide spread that erases the edge → SKIP** (the T30 fix);
  - bid/ask absent → falls back to mid-edge (unchanged behavior);
  - buy-YES vs buy-NO chosen by the executable edge sign.
- `_to_market` parses `rules_primary`/`rules_secondary` into `Market.rules` (and empty when absent).
- `debate()` prompt contains the resolution rules, the bid/ask quote, and the market-as-prior
  instruction (assert via a recording fake client).
- Existing roundtable + floor tests stay green (update assertions only where the reason
  string / prompt text intentionally changed).

## Out of scope (Phase B)
The trade journal, mark-to-market, the resolver, the vault mirror, and lessons-recall — all
deferred to the Phase B spec. Phase A ships independently and improves decisions on its own.
