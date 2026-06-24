# Exit Layer — Position Management & Profit Realization (design)

**Date:** 2026-06-23
**Status:** approved design, pending implementation
**Repo:** `~/council`, branch TBD (off `main`)
**Predecessor:** Phase A (smarter decisions), Phase B (learning loop / journal), and the Scout Funnel are merged. The scout model was switched to Gemini 3.1 Flash-Lite (cheap, non-reasoning).

## Goal

Let the bot **realize** moves instead of round-tripping correct calls back to zero. Today the bot only ever *enters* a position and holds it to settlement; there is no sell/exit path of any kind. This layer adds cheap, deterministic exit logic so a position that has converged to fair value (or hit a profit target) is closed and the capital recycled.

## The problem it fixes

- `KalshiTrader` (`trading/execution.py`) has only `place_order` / `build_order` (entries) + `balance`. There is **no way to sell/close a position**.
- `FloorState._council_eval()` places an entry and the position then sits with `ttl=9_999` — `mark_open()` only updates *unrealized* P&L; `resolve_settled()` only realizes P&L when the **Kalshi market itself** settles. Nothing closes a position early.
- Account reconciliation (2026-06-23) showed the real consequence: of the trades that actually executed (manually / in legacy sessions), roughly half settled against the position and were held to zero with no exit. Authoritative realized losses include KXMODELHIGH −$2.05 and KXGTAPRICE −$0.50.

A correct probability estimate earns nothing if the position is never closed when the market agrees with it. This layer is the single highest-leverage change toward profitability.

## Scope (decided)

- **Manages only NEW bot-placed positions** opened from here on. The existing orphan Kalshi positions (e.g. the KXPCECORE short) are left alone — no position-import subsystem in this build.
- **Triggers: edge-decay + take-profit only.** No time-stop. Stop-loss is implemented as a **disabled-by-default knob** (see Known trade-off).
- **Evaluation is pure price math, no LLM calls** — runs every tick at $0 cost using the already-cached live quotes.

### Known trade-off — no hard downside cap (accepted)

Both chosen triggers fire on *favorable* convergence. Edge-decay does **not** protect against the council being wrong: when the market moves against a long position the price falls, which makes `fair_value − price` *larger*, i.e. edge-decay sees *more* edge and holds. So a genuinely wrong call still rides to settlement, exactly as today. This is an accepted limitation for the first build. Mitigation: `EXIT_STOP_LOSS_CENTS` is wired through the decision core but **disabled by default** (`None`), so enabling a hard stop later is a config change, not a code change.

## Architecture

Four isolated units, following the existing `scout.py` / `decide()` separation of a pure decision core from the I/O shell.

### 1. `trading/exits.py` — pure decision core (no I/O)

```python
@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str            # "" when not exiting

def exit_signal(position, quote, params) -> ExitDecision
```

- `position`: the open position — exposes `side` ("yes"/"no"), `entry_price` (cents), `fair_value` (council converged probability ×100, cents).
- `quote`: live market quote — exposes `yes_bid`, `yes_ask`, `no_bid`, `no_ask` (cents).
- `params`: `ExitParams(take_profit_cents, exit_edge_cents, stop_loss_cents | None)`.

**Long-YES** (we sell into `yes_bid = b` to close; entry `e`, fair value `f`):
- Take-profit: `b - e >= take_profit_cents` → exit, reason `"take-profit +{b-e}c"`
- Edge-decay: `f - b <= exit_edge_cents` → exit, reason `"edge decayed ({f-b}c <= {exit_edge_cents}c)"`
- Stop-loss (only if `stop_loss_cents is not None`): `e - b >= stop_loss_cents` → exit, reason `"stop-loss -{e-b}c"`

**Long-NO** is symmetric: sell into `no_bid`, entry the no-price, fair value `100 - f`.

Pure, deterministic, side-effect-free → exhaustively unit-testable. Evaluating **either** chosen trigger firing means exit (logical OR).

### 2. Journal — position lifecycle

The `trades` table already has every column needed (`side`, `converged_p`, `fill_price`, `contracts`, `status`, `last_mark_price`, `unrealized_pnl`, `outcome`, `realized_pnl`). Add three methods:

- `record_open(...)` — insert a row with `status='open'`, capturing `fill_price` (entry), `converged_p` (fair value), `contracts`, `side`, `market_id`/`series`.
- `open_positions() -> list[Position]` — `SELECT … WHERE status='open'`.
- `record_exit(position_id, exit_price, fee, fill_count)` — set `status='exited'`, compute `realized_pnl = (exit_proceeds − entry_cost − fees)`, stamp `resolved_ts`. Feeds calibration + the Obsidian mirror automatically.

An intermediate `status='exiting'` guards against double-firing (see Error handling).

### 3. `KalshiTrader` — add a close path

Extend `build_order` / `place_order` to carry an explicit `action` ("buy" | "sell") so a long-YES exit is `action="sell", side="yes"` at the bid. Additive only — existing entry call sites keep working (default `action="buy"`).

### 4. `floor.py` — wiring

- **Entry side:** in the entry-placement branch of `_council_eval`, after a successful `place_order`, call `journal.record_open(...)`.
- **Exit side:** new `_exit_eval()` invoked each tick (alongside the existing deliberation cadence). For each `journal.open_positions()`:
  1. Look up the quote in the **already-cached** `self._live_markets` (no new API call).
  2. `sig = exit_signal(position, quote, self._exit_params)`.
  3. If `sig.should_exit and self.live`: mark `exiting`, place the close order (cross the spread to the bid), then `journal.record_exit(...)` on fill and mirror to Obsidian.

## Data flow

```
entry:  council decide() != SKIP → place BUY → journal.record_open()
tick:   for p in journal.open_positions():
            q = self._live_markets.get(p.market_id)
            if q is None: continue                       # can't price → skip
            sig = exit_signal(p, q, self._exit_params)
            if sig.should_exit and self.live:
                journal mark p 'exiting'
                place SELL (cross spread)  →  journal.record_exit()  →  Obsidian mirror
```

## Error handling / safety

- **Exits bypass the daily-loss kill switch.** Closing reduces exposure, so an exit must be allowed even when `RiskGuard` has halted *entries*. The exit path does not call `guard.check()`.
- **No double-exit.** Once a close order is placed, the position is marked `exiting`; `open_positions()` excludes it, so the next tick won't re-fire before the fill is confirmed.
- **Missing quote** (market closed/delisted/illiquid): skip that position this tick — cannot compute an exit price.
- **Rejected close order:** log and leave the position `open` (revert from `exiting`); retry next tick.
- **Partial fill:** decrement remaining `contracts`; keep the position `open` for the remainder.

## Configuration (env, tunable)

| Var | Default | Meaning |
|---|---|---|
| `EXIT_TAKE_PROFIT_CENTS` | `5` | Close when the position is up this many cents vs entry. |
| `EXIT_EDGE_CENTS` | `1` | Close when remaining edge to fair value `<=` this (market has converged). |
| `EXIT_STOP_LOSS_CENTS` | unset (disabled) | If set, close when the position is down this many cents vs entry. |

With both default triggers on, the bot exits at whichever fires first — usually the 5¢ take-profit before full convergence. Deliberately conservative (locks small, frequent gains) for a first *profitable* build; all three are knobs.

## Testing (TDD)

- **`exits.py` (pure):** take-profit fires; edge-decay fires; neither fires; long-YES vs long-NO symmetry; threshold boundaries (exactly at vs just under); stop-loss off vs on; missing/zero quote handled by caller (function assumes a valid quote).
- **Journal:** `record_open` → `open_positions` round-trip; `record_exit` realized-P&L arithmetic (proceeds − cost − fees); `exiting` excluded from `open_positions`.
- **`KalshiTrader`:** sell-order body shape is correct (no live network call — assert the built payload).
- **Floor integration (mock market + mock trader):** open a position, move the mock quote so a trigger fires, assert the close order is placed and the position transitions `open → exiting → exited` with correct realized P&L; assert exits are attempted even when the daily-loss guard has halted entries.

## Out of scope (YAGNI)

- Importing / managing pre-existing orphan Kalshi positions.
- Time-based exits.
- Re-running the council to re-estimate fair value mid-hold (would reintroduce the per-tick LLM cost just removed from the scout).
- Trailing stops, scaling out partially, averaging down.
