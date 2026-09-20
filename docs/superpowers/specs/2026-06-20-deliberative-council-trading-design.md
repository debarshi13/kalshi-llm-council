# Phase 2 — Deliberative Council Trading (design)

**Date:** 2026-06-20
**Status:** approved design, pending spec review
**Repo:** `~/council`
**Predecessor:** Phase 1 (live execution wiring) + the 2026-06-20 floor-observability fix.

## Goal

Replace the three independent single-model "books" with **one council** that, per market,
runs a short **debate** among Claude, Kimi K2, and GLM-5.2, converges on a probability,
and **autonomously** places or skips a real Kalshi order — no per-trade human ship/deny.
The floor UI shows the debate happening in real time.

## Decisions locked during brainstorming

| # | Decision | Choice |
|---|----------|--------|
| 1 | Deliberation shape | **Debate & converge**: round 1 independent estimates → round 2 each sees others & revises once → aggregate |
| 2 | Place/skip rule | **Deterministic consensus**: place iff `|mean edge| ≥ EDGE_THRESHOLD` AND `spread ≤ SPREAD_CAP`; size scales with edge, clamped to caps. No model pulls the trigger. |
| 3 | Research feed | **Lightweight web search**: one search per market → shared snippets injected into round-1 prompts |
| 4 | Desk structure | **Replace** A/B/C with one deliberating council (only real-money path) |
| 5 | Cadence | **Scan & pick the best**: cheap-rank watchlist (no LLM) → one full debate on the top candidate per interval; daily trade cap |
| 6 | UI | Show the live debate on the floor; polish with **anime.js** (CDN) + dark-OLED aesthetic |
| 7 | Search backend | **OpenRouter `:online`** suffix (no extra key) for the single research call; pluggable for a dedicated search API later |

## Architecture

```
_council_eval (every DELIBERATE_EVERY ticks, if calls/trades under caps):
  1. rank candidates   cheap_score(market) over live watchlist, no LLM → pick top unseen
  2. research          ResearchProvider.context_for(market) → shared notes (1 call)
  3. debate            DeliberativeCouncil.debate(market, notes) → Deliberation
                         round 1: 3 models estimate P(YES)+thesis (notes only)
                         round 2: 3 models revise after seeing the others' r1
  4. converge          mean P(YES), spread = stdev
  5. decide            decide(deliberation, market, caps) → Decision (pure)
  6. execute           if Decision.place:
                         armed → RiskGuard.check → KalshiTrader.place_order (REAL)
                         not armed → paper fill (existing ledger path)
  7. log               full transcript + decision → activity feed + Obsidian
```

The execute seam (step 6) reuses the **existing** `floor._auto_execute` → `RiskGuard` →
`KalshiTrader.place_order`. No change to the real-money path or the caps.

## New components (isolated, testable)

### `src/council/trading/deliberation.py`
- `@dataclass ModelEstimate{ model: str, p_yes: float, thesis: str }`
- `@dataclass Deliberation{ market_id, round1: list[ModelEstimate], round2: list[ModelEstimate],
  converged_p: float, spread: float, notes: str }`
- `@dataclass Decision{ place: bool, side: "yes"|"no"|None, contracts: int,
  limit_price_cents: int, reason: str }`
- `class DeliberativeCouncil`:
  - `__init__(self, specs: list[ModelSpec], client: ModelClient)`
  - `debate(self, market: Market, notes: str) -> Deliberation` — runs both rounds. Round 2
    prompt includes each peer's `(model, p_yes, thesis)`. Reuses the defensive parser from
    `analysts.parse_estimate` (defer to market price on unparseable output).
- `def decide(d: Deliberation, market: Market, caps: RiskGuard,
  edge_threshold: float, spread_cap: float) -> Decision` — **pure**:
  - `edge = d.converged_p - market.yes_price`; `side = "yes" if edge>0 else "no"`
  - `place = abs(edge) >= edge_threshold and d.spread <= spread_cap`
  - `entry = market.yes_price if side=="yes" else round(1-market.yes_price,2)`
  - `contracts = clamp(floor(caps.max_position_usd / max(entry,0.05)), 1, …)` so cost ≤ position cap
  - `reason` summarizes the gate result for the log (e.g. "8.7¢ edge, spread 0.018 ≤ 0.05 → PLACE NO").

### `src/council/trading/research.py`
- `class WebSearchResearch(ResearchProvider)` (satisfies the existing `analysts.ResearchProvider` protocol):
  - `context_for(self, market: Market) -> str` — one OpenRouter `:online` call asking for
    3–5 dated, sourced snippets relevant to the market title; returns plain text. Failures
    degrade to `"No external signal available."` (never raise into the loop).
- Backend is injected (a `search_fn`) so tests pass a fake and no network is touched.
- `MockResearch` (existing) stays the default when no backend is configured.

## Integration changes

- `src/council/trading/floor.py`:
  - Replace `_analysts` (per-book) + `_live_eval` with a single `DeliberativeCouncil` +
    `_council_eval`. `enable_live()` builds the council + `ResearchProvider`.
  - Add `cheap_score(market)` ranking (volume, price-not-extreme, recency; no LLM) and a
    "recently deliberated" set so it rotates instead of re-debating the same market.
  - Add cadence/caps: `DELIBERATE_EVERY`, `MAX_TRADES_PER_DAY` (new), keep `MAX_LIVE_CALLS`.
  - `snapshot()` gains a `debate` field: the current/last `Deliberation` (per-model rounds,
    converged P, spread, decision) for the UI to render. Backward-compatible (additive).
- The independent-book mock path (`_mock_gen`) stays for AUTO-off-LIVE demo at zero cost.

## UI — live debate on the floor (`web/floor-game.html`)

Builds on the observability fix already shipped (the activity feed + event-driven workers).

- **Debate Theater panel** above/with the activity log: when `snapshot.debate` is present,
  render the three members as columns — each shows round-1 P(YES) and thesis, then round-2
  (revised) value, then the converged mean + spread, then a **PLACE/SKIP stamp** with the side
  and reason. This is the literal "what the AIs are discussing".
- **Canvas choreography:** on a new debate, the three workers gather at THE PIT (war-room);
  speech bubbles show their actual round-1 then round-2 lines (driven by `snapshot.debate`,
  not RNG — extends `reactToActivity`).
- **anime.js (CDN)** for the key beats only (≤1–2 animations per beat, ease-out/in,
  transform/opacity only):
  - staggered reveal of the three columns (round 1),
  - number-tween of each P(YES) on revise (round 2),
  - a "convergence" beat where the three values slide toward the mean line,
  - the decision stamp scale/opacity-in.
- **Accessibility/perf:** gate all anime.js timelines behind `prefers-reduced-motion: no-preference`;
  keep dark-OLED palette (green up / red down / amber pending), visible focus, 4.5:1 contrast,
  SVG icons (no emoji). Fonts unchanged (IBM Plex Mono + Bricolage Grotesque).

## Config / tuning (env, with config.yaml fallback)

`EDGE_THRESHOLD=0.06`, `SPREAD_CAP=0.05` (stdev), `MAX_TRADES_PER_DAY` (default 10),
`DELIBERATE_EVERY` (ticks), `COUNCIL_RESEARCH=online|mock` (selector). Risk caps unchanged
(`MAX_POSITION_USD=5`, `MAX_TOTAL_USD=50`, `MAX_DAILY_LOSS_USD=20`) + kill switch.

## Autonomy & safety

- Autonomous mode = **LIVE on + ARM on** (requires `COUNCIL_MODE=live`). No per-trade gate.
- Guardrails: `RiskGuard` ($5/$50/$20) + kill switch + `MAX_TRADES_PER_DAY` + token pacing
  (one debate/interval, `MAX_LIVE_CALLS` backstop). Every autonomous order logged to the feed
  and the vault.
- `place_order` body remains **unverified against a real Kalshi order** — unchanged risk from
  Phase 1; the first real order is still the live test. Caps are the protection.
- No edge proven; EV likely negative. User accepts the risk (their account/money).

## Testing (offline, no keys — matches the existing ~67 tests)

- `test_deliberation.py`: `FakeModelClient` scripts round-1/round-2 replies →
  - consensus → **place** (correct side, size within cap),
  - high spread → **skip** ("no consensus"),
  - sub-threshold edge → **skip**,
  - mixed sides resolving to small mean edge → **skip**,
  - round-2 actually feeds peers' r1 into the prompt (assert prompt contents).
- `decide()` pure-function table across edge × spread combinations (boundary at threshold/cap).
- `test_research.py`: `WebSearchResearch` with a fake `search_fn` (success + raising → graceful
  fallback string).
- Integration: `_council_eval` with fake client + **fake trader** → asserts `RiskGuard.check`
  runs and `place_order` is called with the decided ticker/side/size; zero real orders/spend.
- `MAX_TRADES_PER_DAY` halt path.

## Files

- **New:** `trading/deliberation.py`, `trading/research.py`,
  `tests/test_deliberation.py`, `tests/test_research.py`.
- **Modified:** `trading/floor.py` (council eval, snapshot.debate, caps),
  `web/floor-game.html` (debate theater + anime.js), config plumbing for the new knobs.
- **Retired:** the per-book live `_analysts` path in `floor.py` (mock demo path stays).

## Out of scope (later phases)

Dedicated search-API backend; multi-market parallel debates; learning/tuning thresholds from
realized P&L; demo-host order verification (separate task); the suspend-hang fix needed before
true unattended overnight running ([[gpu-suspend-hang]]).

## Open items for spec review

1. Confirm `:online` as the research backend (vs. wiring a Tavily/Brave key).
2. Confirm debate depth = exactly 2 rounds (1 revision).
3. Confirm `MAX_TRADES_PER_DAY` default (10) and `DELIBERATE_EVERY` interval.
