# Blend, Don't Beat — Kalshi Bot Edge Rework

**Date:** 2026-07-04 · **Status:** approved (brainstorming) · **Goal:** find real edge · **Validation:** calibration-first

## Problem

The floor has never placed a winning strategy's trade. Evidence:
- `data/journal.sqlite`: 27 council debates, 27 SKIPs — converged_p ≈ market price every time (edges of 0.0–2.3¢ against a 3¢ threshold).
- Real account: $27.31 → ~$17.86 across 19 real fills (6W / 7L / 5 flat) from the earlier taker-order phase.
- Root causes (all in code): (1) `cheap_score` selects liquid, near-50¢, fast-closing markets — maximum price efficiency and maximum Kalshi taker fee (1.75¢ at 50¢); (2) the roundtable prompt anchors models to the market price as a "STRONG PRIOR" and models speak sequentially seeing peers — herding by construction; (3) orders cross the spread as takers, paying full fees on both sides.
- The vault's own assessment (`The Brain/wiki/sources/Will the Council Trading Bot Be Profitable 2026-06-20.md`): directional LLM-vs-price trading is structurally negative-EV; credible paths are illiquid-market blending, maker orders, tails, fee-aware thresholds, calibration correction.

## Design

### 1. Market selection — hunt where the price is weak (`floor.py`, `scout.py`)

Replace `cheap_score`'s liquidity bias with `edge_score`:
- **Tails:** prefer prices in 0.03–0.20 and 0.80–0.97 (fees ~3x cheaper; documented favorite-longshot bias). Price filter in `_load_live_markets` widens from 0.05–0.95 to 0.03–0.97.
- **Under-followed:** volume must clear `MIN_VOLUME` (fillability floor) but heavy volume is *penalized*, not rewarded — the price of a heavily traded market already embeds the sharps.
- **Maker-viable spread:** reward spreads wide enough to post inside (≥2¢), cap at a maximum (default 15¢) beyond which fills are unrealistic.
- **Resolution horizon:** prefer markets resolving within `EDGE_HORIZON_DAYS` (default 7) so calibration data accrues; exclude sub-hour lotteries (BTC hourly strikes).
- `scout.structural_score` gets the same retune: tail + spread components stay, `vol_score` flips to an under-followed score. Per-series diversity cap unchanged.

### 2. Debate — independent signal, then convergence (`deliberation.py`)

- **Round 1 (blind):** each model receives title + resolution rules + research notes — **no market price, no peer messages**. Output: P(YES) + 2-sentence thesis. Fills the currently unused `round1` field.
- **Round 2 (converge):** reveal the market price, the live quote, and all round-1 estimates; each model may revise with a stated specific reason. The "market price is a STRONG PRIOR — converge toward it" instruction is removed; replaced with "the price is one input; deviate only with a specific reason, but do not defer to it reflexively."
- **Aggregation:** median of round-2 estimates (robust to one outlier), then logit-space extremization: `p' = σ(α·logit(p))`, α default 1.3, env `EXTREMIZE_ALPHA`, later fitted from journal calibration buckets.
- **Cost:** 6 calls/debate (2 rounds × 3 models) vs today's 3; the scout funnel already caps debates (~1 per eval cycle), and `MAX_LIVE_CALLS` stays as the session backstop.

### 3. Decision & execution — maker orders, fee-aware gate (`decide()`, `execution.py`)

- **Fee-aware threshold** replaces flat `EDGE_THRESHOLD`: `required_edge(p) = maker_entry_fee(p) + SPREAD_BUFFER (default 1¢) + MIN_PROFIT (default 1¢)`. Uses the published Kalshi fee formula at the *entry price*, maker rate (25% of taker). Settlement is fee-free, so no exit fee in the base case; an early exit pays taker fees but is loss-reduction, already handled by the exit layer.
- **Maker execution:** instead of crossing, post a resting limit inside the spread (best bid + 1¢ for YES; symmetric for NO). New `KalshiTrader.cancel_order(order_id)` + open-order tracking in the floor; unfilled resting orders cancel after `ORDER_TTL_TICKS` (default 30). Only booked on actual `fill_count` (existing invariant preserved).
- **Paper mode** simulates maker fills: a resting order fills if the market trades through its price before TTL (approximated from quote movement at mark ticks).
- RiskGuard caps ($5/position, $50 total, $20 daily loss, kill switch) and the exit layer are unchanged; exits may still cross the spread (closing reduces risk — speed over fees).

### 4. Calibration gate — the honesty mechanism (`journal.py`, `floor.py`)

- Journal rows gain `blind_p` (round-1 median) and keep `converged_p`, `market_price` — logged for **every** debate, traded or skipped.
- New `Journal.calibration_report()`: over resolved predictions, realized Brier of converged_p vs Brier of the market price baseline, bucketed by edge size and price region; also blind_p Brier (measures whether round 1 adds signal).
- **Arming lock:** `arm_execution()` raises unless `calibration_report()` shows ≥ `CALIBRATION_MIN_N` (default 50) resolved predictions AND model Brier < market Brier. Override: `COUNCIL_CALIBRATION_OVERRIDE=true` (explicit, logged loudly).
- Weekly calibration summary mirrored to the Obsidian vault (`wiki/sources/`), alongside existing per-trade mirrors.

## Testing

Offline-first, existing pattern: MockScout/MockCouncil/MockMarketData grow blind-round and maker-fill behaviors; new pure functions (`edge_score`, `required_edge`, extremization, maker-fill simulation, calibration report) get direct unit tests. Full suite must stay green without API keys.

## Config surface (new/changed env)

`EDGE_HORIZON_DAYS=7`, `EXTREMIZE_ALPHA=1.3`, `SPREAD_BUFFER=0.01`, `MIN_PROFIT=0.01`, `ORDER_TTL_TICKS=30`, `CALIBRATION_MIN_N=50`, `COUNCIL_CALIBRATION_OVERRIDE=false`. Removed: flat `EDGE_THRESHOLD` (superseded by fee-aware gate).

## Out of scope (deferred)

Polymarket cross-venue divergence feed (phase 2 — plugs into the same fee-aware gate), market-making (two-sided quoting), automated bankroll scaling, non-binary contracts.

## Success criteria

1. Bot debates markets where its probability *can* differ from price (blind round-1 spread vs price > 0 on average).
2. ≥50 resolved paper predictions accumulate within ~2–3 weeks of running.
3. Honest verdict either way: calibration report shows model Brier vs market Brier; live arming unlocks only on a win.
4. If/when armed: maker fills with round-trip fees ≤ 25% of taker equivalent.
