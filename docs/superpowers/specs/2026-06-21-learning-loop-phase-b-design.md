# Phase B — The Learning Loop / "The Brain" (design)

**Date:** 2026-06-21
**Status:** design (core decisions locked in the 2026-06-21 brainstorm), pending spec review
**Repo:** `~/council`, branch `feat/deliberative-council`
**Predecessor:** Phase A (smarter decisions) — shipped.

## Goal
Give the council a memory so it learns from its own record: log every decision, score every
outcome, and feed that experience back into future debates — so it stops repeating mistakes
(misreads, over-confident edges) instead of just being fenced off from them.

## Locked decisions (from brainstorming)
- **Learning signal:** both — log the decision immediately + an ongoing **mark-to-market**, and
  **backfill the true outcome** when the market resolves.
- **Storage:** a **structured journal** in `~/council` (SQLite, like the existing run history) is
  the source of truth the council reads; **mirror** each trade as a human-readable note in the
  Obsidian "brain".
- **Recall:** inject **both** similar past trades (same series) **and** calibration stats into
  each debate.

## Components (each isolated + testable)

### 1. `trading/journal.py` — the store
SQLite (reuse the `artifacts.py` pattern) at `data/journal.sqlite`. One `trades` table:
`id, opened_ts, market_id, series, title, side, converged_p, spread, market_price,
executable_price, edge, contracts, fill_price, fill_count, decision_reason, rationale,
status('placed'|'resolved'|'skipped'), last_mark_price, last_mark_ts, unrealized_pnl,
outcome('yes'|'no'|null), realized_pnl, council_correct(bool|null), resolved_ts`.
`series` = the ticker family (prefix before the first dated/strike segment, e.g.
`KXHORMUZWEEKLY`) for similar-trade recall.
API (pure, dependency-injected clock so tests are deterministic):
- `log(decision, deliberation, market, fill) -> id` — write a `placed` (or `skipped`) row.
- `mark(trade_id, yes_price)` — update `last_mark_*` + `unrealized_pnl`.
- `resolve(market_id, outcome) -> n` — settle all open rows on that market: compute
  `realized_pnl` (contracts × (payout − cost) − fee) and `council_correct` (did the taken side win?).
- `recall(market) -> Lessons` — see §5.
- `calibration() -> dict` — aggregate over resolved rows (see §5).

### 2. Logging hook — `floor._council_eval`
After a fill (or a SKIP), call `self.journal.log(...)` with the Deliberation, Decision, market,
and the fill result. `rationale` = each model's final line + the verdict reason (already in hand).
SKIPs are logged too (status `skipped`) so the journal records what it *passed* on.

### 3. Marker — periodic mark-to-market
A `mark_open()` method: for each open trade, fetch the current price (`KalshiMarketData.get_market`)
and `journal.mark(...)`. Called from the tick loop on a cadence (e.g. every `MARK_EVERY` ticks).

### 4. Resolver — periodic truth backfill
A `resolve_settled()` method: for each open trade's market, check status via
`KalshiMarketData.get_market` (`status=="resolved"`, `outcome` set); when settled, call
`journal.resolve(market_id, outcome)`. Called from the tick loop on a slow cadence.

### 5. Recall — `journal.recall(market) -> Lessons`
Builds a **LESSONS** block injected into the debate:
- **Similar trades:** the last 5 resolved trades on the same `series`, with outcomes and a
  one-line common-error note ("KXHORMUZWEEKLY: 1W/3L — repeatedly misread the >T threshold").
- **Calibration:** over all resolved trades — hit rate by edge bucket (`<5¢, 5–15¢, 15–25¢, >25¢`),
  by spread bucket, and overall (n + win%). E.g. "your >25¢ edges: 1/8 correct — be skeptical."
- Returns a short text block (capped length); empty/"no history yet" when the journal is sparse.

### 6. Recall injection — `DeliberativeCouncil.debate(market, notes, lessons="")`
`debate()` gains an optional `lessons` param; `_market_block` adds a `LESSONS (your own track
record):` section when present. `floor._council_eval` calls `lessons = journal.recall(m)` BEFORE
the debate and passes it in.

### 7. Vault mirror — `trading/journal_mirror.py`
On `log` and on `resolve`, write/update `<vault>/wiki/trades/<market_id>.md` (reuse the
`obsidian.py` write pattern) with the decision, rationale, marks, and final outcome — so the
track record is browsable in the brain. Mirror failures never break trading (best-effort,
logged). Vault path comes from the existing Obsidian config the project already uses
(`config.yaml` `obsidian.vault` / its env override in `config.py`); mirroring is skipped if
no vault is configured. Confirm the exact key against `config.py`/`obsidian.py` at build time.

## Data flow
`recall(market) → debate(notes, lessons) → decide → fill → journal.log → [tick] mark_open →
[tick] resolve_settled → calibration + similar feed the next recall; mirror writes notes.`

## Integration changes
- `floor.py`: construct `self.journal` in `__init__`; `recall` before debate; `log` on
  decision; `mark_open`/`resolve_settled` in `tick()` on cadences (`MARK_EVERY`,
  `RESOLVE_EVERY`, env-tunable).
- `deliberation.py`: `debate(..., lessons="")` + `_market_block` LESSONS section.

## Testing (offline, no keys/network — matches the 80-test suite)
- Journal lifecycle with in-memory SQLite + injected clock: `log → mark → resolve` updates rows
  correctly; `realized_pnl` and `council_correct` computed right for YES-wins and NO-wins.
- `recall`: returns the right similar trades (same series only) + correct calibration buckets;
  empty-history case returns a "no history" block.
- Resolver with a fake `KalshiMarketData` that flips a market to resolved → trade settles.
- `debate()` prompt contains the LESSONS block when `lessons` is passed (recording fake client).
- Mirror writes a note to a temp dir; mirror exception doesn't propagate.
- All existing 80 tests stay green (journal is additive; default no-op when no DB/vault).

## Out of scope
Auto-tuning thresholds from calibration (the council *reads* its stats but humans still set the
gate); closing/managing open positions; cross-session embeddings for "semantic" similarity
(series-prefix matching only for now).

## Suggested build order (for the plan)
1. `journal.py` store + log/mark/resolve (pure, tested).
2. `recall` + calibration.
3. floor logging hook + `debate(lessons=...)` injection.
4. marker + resolver in the tick loop.
5. vault mirror.
