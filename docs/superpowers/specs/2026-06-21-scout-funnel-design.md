# Scout Funnel — Cheap Pre-Screen Before Council Debate (design)

**Date:** 2026-06-21
**Status:** approved design, pending implementation
**Repo:** `~/council`, branch TBD (off `main`)
**Predecessor:** Phase A (smarter decisions) and Phase B (learning loop / journal) are already merged.

## Goal

Cut wasted frontier-model spend by 10-20x on intervals that end in SKIP. Replace the
current "always debate the top market" flow with a 3-tier funnel that only convenes the
expensive 3-model council when a cheap scout flags a genuinely promising candidate.

## The problem it fixes

`FloorState._council_eval()` (floor.py L196-252) picks `max(pool, key=self.cheap_score)`
and immediately runs a full roundtable debate — 1 web-search call + 3 model turns (Claude,
GPT-5.4, Kimi K2) via `self.council.debate(m, notes, lessons)`. Most intervals end with
`decide()` returning SKIP because liquid markets are efficiently priced. Each wasted debate
costs ~4 frontier-model API calls. The council debates one market per interval; if it SKIPs,
those tokens bought zero information.

**Token math today:** a skip costs ~4 frontier calls (1 research + 3 roundtable turns).
After the funnel, a skip costs ~1 cheap Kimi K2.6 call — roughly 10-20x cheaper, since
Kimi is ~4x cheaper per token than GPT-5.4/Claude and the scout prompt is shorter than a
full debate turn.

## Design — 3-tier funnel

### Tier 0 — Structural pre-filter (free, no tokens)

Pure-Python scoring that extends/replaces the current `cheap_score(m)` (floor.py L187-194).
Rank candidate markets by deterministic mispricing signals:

1. **Stale-price-vs-volume** — price barely moved but `volume / 24h` spiked, suggesting a
   slow book that hasn't repriced. (The current `cheap_score` already captures volume/hours,
   but doesn't compare price staleness.)
2. **Favorite-longshot tails** — `yes_price < 0.10` or `yes_price > 0.90`. These extremes
   have a documented behavioral bias (bettors overpay for longshots, underprice near-certs);
   worth a closer look even if volume is modest.
3. **Wide bid/ask spread** — `yes_ask - yes_bid > threshold`. A wide spread can mean the
   market is mispriced or illiquid; either is interesting for a scout (illiquidity is filtered
   later by `decide()` / `SPREAD_CAP`).
4. **Closing-soon recency** — markets resolving within hours have the most actionable
   information asymmetry (news is fresh, the book may lag).

Produces a **ranked shortlist** of up to `SCOUT_SHORTLIST` markets (default 8) from the
current pool. This replaces the single `max(pool, key=self.cheap_score)` pick.

### Tier 1 — Cheap comparative LLM scout (one Kimi K2.6 call)

A single batched LLM call feeds the entire Tier-0 shortlist to the cheapest available model
(Kimi K2.6 via `openrouter/moonshotai/kimi-k2.6`) and asks it to identify the top 1-2
markets that look genuinely mispriced — or NONE.

**Prompt requirements:**
- For each shortlisted market: ticker, title, yes_price, yes_bid, yes_ask, volume, hours to
  close, and a one-line summary (from `market.rules` truncated).
- The scout must **name a specific catalyst** (what the market is missing) to flag a market.
  Absent a concrete reason, it must return NONE.
- **Brain-aware:** inject `journal.calibration()` stats and `journal.recall(market_id)`
  lessons for each candidate, so the scout avoids historically-losing trade types (e.g.
  "your >25c edges are 1/5 correct — discount large divergences").

**Why Kimi over GPT/Claude:** cost. This call runs every interval (every `DELIBERATE_EVERY`
ticks). Kimi K2.6 is ~4x cheaper per token than GPT-5.4 and triage doesn't need top-tier
reasoning. Save Claude + GPT for the confirmed debate where reasoning quality matters.

**Output parse:** the scout returns a structured list of flagged market IDs (or an empty
signal for NONE). Defensive parsing — if the response is malformed or unparseable, treat it
as NONE (fail closed; never escalate on a parse error).

**Escalation:** at most `SCOUT_MAX_ESCALATE` markets (default 1) proceed to Tier 2.

### Tier 2 — Full council debate (existing roundtable)

The existing 3-model roundtable (`DeliberativeCouncil.debate()` + `decide()`) runs **only**
on a Tier-1-flagged candidate. If Tier 1 flags nothing, no debate fires that interval.

No changes to the debate or decision logic itself — Phase A already hardened that path.

## New files and components

### `src/council/trading/scout.py` — the `Scout` class

```python
class Scout:
    """Three-tier funnel: structural filter -> cheap LLM triage -> escalate."""

    def __init__(self, client: ModelClient, model: str, shortlist_n: int, max_escalate: int):
        ...

    def shortlist(self, markets: list[Market]) -> list[Market]:
        """Tier 0: pure-Python structural ranking. No API calls.
        Returns up to self.shortlist_n markets, best-first."""
        ...

    def pick(self, markets: list[Market], journal: Journal) -> list[Market]:
        """Tier 1: one cheap LLM call over the Tier-0 shortlist.
        Returns 0..max_escalate markets flagged as genuinely mispriced, or [].
        Injects journal calibration + per-market recall as context."""
        ...
```

**Key properties:**
- `shortlist()` is a pure function (deterministic, no side effects, no API calls). Testable
  with synthetic `Market` objects.
- `pick()` takes a `Journal` (not a lessons string) so it can call both `journal.calibration()`
  and `journal.recall(m.id)` per candidate. It uses the injected `ModelClient` with a
  `ModelSpec(slug=self.model, goal="scout")`.
- `pick()` defensive-parses the LLM response: extracts market IDs via regex/JSON, validates
  each against the input list, caps at `max_escalate`. On any parse failure, returns `[]`.

### `src/council/trading/scout.py` — `structural_score(m: Market) -> float`

Module-level pure function (or a method on `Scout`) that computes the Tier-0 composite
score. Replaces/extends the current `cheap_score`. Weights TBD during implementation (start
with equal weights, tune from journal data later).

## Integration changes — `floor.py`

**In `FloorState.__init__`:**
- Read new env vars: `SCOUT_MODEL`, `SCOUT_SHORTLIST`, `SCOUT_MAX_ESCALATE`.
- Instantiate `self.scout = Scout(client, model, shortlist_n, max_escalate)`.
  In mock/paper mode, `Scout` with a mock `ModelClient` (Tier 1 returns `[]` or random).

**In `FloorState._council_eval()` (the main change):**

Replace:
```python
m = max(pool, key=self.cheap_score)
# ... debate + decide on m
```

With:
```python
shortlist = self.scout.shortlist(pool)
escalated = self.scout.pick(shortlist, self.journal) if shortlist else []
if not escalated:
    self._log("Scout: no candidates escalated — skipping debate")
    return
m = escalated[0]  # debate the top escalated candidate
# ... existing debate + decide on m (unchanged)
```

The `self.calls` counter increments by 1 for the scout Kimi call (when `self.live`).

**In `FloorState.enable_live()`:**
- Build the scout's `ModelClient` (can share the same `ModelClient` instance for budget
  tracking) and real `Scout`.

**Backward compatibility:** `SCOUT_MODEL` defaults to the Kimi slug so new deployments get
the funnel automatically. Setting `SCOUT_MODEL=""` (empty string) disables the scout and
falls back to the current behavior (pick top by `cheap_score`, always debate), so existing
deployments that explicitly opt out still work.

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `SCOUT_MODEL` | `openrouter/moonshotai/kimi-k2.6` | LiteLLM model slug for the Tier-1 scout call. Set to empty string to disable the scout and fall back to the old single-market `cheap_score` path. |
| `SCOUT_SHORTLIST` | `8` | How many markets Tier 0 passes to Tier 1 |
| `SCOUT_MAX_ESCALATE` | `1` | Max markets Tier 1 can escalate to Tier 2 per interval |

These are read in `FloorState.__init__` via `os.environ.get(...)`, consistent with the
existing config pattern (`EDGE_THRESHOLD`, `SPREAD_CAP`, `DELIBERATE_EVERY`, etc.).

## Testing (offline, no API keys)

All tests run offline with synthetic `Market` objects and a fake `ModelClient` — matching
the existing 75+ test suite pattern.

### Tier 0 (`structural_score` / `shortlist`)
- **Deterministic ranking:** given a fixed list of markets with known prices, volumes,
  bid/ask spreads, and close times, assert the shortlist order matches expectations.
- **Tail detection:** markets at `yes_price=0.05` and `yes_price=0.95` rank higher than
  mid-priced markets of similar volume.
- **Stale-price-vs-volume:** a market with high volume but stable price ranks above one
  with proportional movement.
- **Shortlist cap:** with `SCOUT_SHORTLIST=3` and 10 candidates, only 3 are returned.
- **Empty pool:** `shortlist([])` returns `[]`.

### Tier 1 (`pick`)
- **Fake ModelClient:** subclass `ModelClient`, override `_invoke` to return canned scout
  responses. Test both "flag market X" and "NONE" responses.
- **Brain injection:** assert the prompt sent to the model contains `journal.calibration()`
  stats and per-market `journal.recall()` lessons (inspect the messages list passed to the
  fake client).
- **Defensive parse — valid JSON:** scout returns `{"escalate": ["TICKER-123"]}` — `pick()`
  returns the matching `Market`.
- **Defensive parse — malformed:** scout returns garbage text — `pick()` returns `[]` (fail
  closed).
- **Defensive parse — hallucinated ticker:** scout returns a market ID not in the input list
  — `pick()` filters it out.
- **Max escalate cap:** scout tries to flag 5 markets but `SCOUT_MAX_ESCALATE=1` — only the
  first valid one is returned.
- **Empty shortlist:** `pick([], journal)` returns `[]` without making an API call.

### Integration (`_council_eval` with scout)
- **Full funnel — escalated:** mock scout flags one market. Assert `council.debate()` is
  called exactly once, on the flagged market.
- **Full funnel — nothing escalated:** mock scout returns `[]`. Assert `council.debate()` is
  **never** called. Assert the activity log contains "no candidates escalated."
- **Call counter:** in live mode, assert `self.calls` increments by 1 for the scout call
  (not by 4).
- **Backward compat:** with `SCOUT_MODEL=""`, assert the old single-market `cheap_score`
  path still works (scout disabled, no regression).

### MockScout (for paper mode)
- Provide a `MockScout` that mirrors `MockCouncil`: deterministic, no API calls, returns
  a random subset of the shortlist (or `[]`) so the paper-mode floor still produces
  activity for the UI to display.

## Out of scope (Phase C follow-on)

- **External reference-price divergence scout** — comparing Kalshi prices against
  Polymarket, PredictIt, futures, or sports odds. This is the highest-value signal (cross-
  exchange arb detection) but requires a new data feed, rate-limited scraping or API
  integration, and price normalization. Separate spec, separate data layer.
- **Multi-market debate** — debating 2+ markets in one council session. The current
  roundtable is single-market; batching debates is a different optimization.
- **Dynamic `SCOUT_SHORTLIST` tuning** — auto-adjusting the shortlist size based on
  historical escalation rates. Nice-to-have but premature until we have enough journal data.
- **Scout model rotation / A-B testing** — trying different scout models per interval.
  Useful later for calibrating scout quality vs cost.
