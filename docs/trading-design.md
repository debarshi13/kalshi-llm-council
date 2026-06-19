# Council Trading Harness — Design Spec

**Date:** 2026-06-19 · **Status:** approved (brainstorming) · **Mode:** paper-first, design-for-live
**Vault:** `The Brain/wiki/sources/Council Trading Pivot.md`

## Goal
Re-aim the multi-model council at **Kalshi** trading. Build a **strategy-comparison harness**: three "books" share one pipeline but use different edge logic, each scored on its own paper P&L + calibration, so weeks of data reveal which (if any) has a real, fee-aware edge before any real money is risked.

## Pipeline (per evaluation tick, per market)
**researcher → analyst → risk-manager → human gate → execution**
- **Researcher** gathers signal (web search / data, per book).
- **Analyst** outputs a calibrated probability + thesis, compared to the Kalshi price → mispricing.
- **Risk-manager** sizes vs caps; vetoes if fees/spread eat the edge.
- **Human gate** (dashboard) approves/denies.
- **Execution** — `paper`: simulated fill + ledger entry; `live` (off by default): real Kalshi order.

## Three books
| Book | Analyst edge | Data |
|---|---|---|
| A · news-driven | fresh info should reprice a watched market | web search + news |
| B · cross-source | Kalshi price vs external probability divergence | polls / econ / futures |
| C · calibrated-reasoning | first-principles estimate on thin/illiquid markets | liquidity filter |

Books run independently with separate bankrolls and ledgers.

## Shared infrastructure (this spec's Phase 1 — strategy-independent, offline-testable)
- **Market data adapter** — `MarketData` protocol: `list_markets()`, `get_market(id)`, resolution lookup. `MockMarketData` (deterministic, offline) + `KalshiMarketData` (real, RSA-signed, lazy `httpx`).
- **Paper ledger** — positions, simulated fills, realized/unrealized P&L, **Kalshi fee model** (`ceil(0.07 · contracts · p · (1−p))` per the published formula; configurable), and **scoring**: P&L, hit rate, fee drag, and **Brier score / calibration** (predicted prob vs outcome).
- **Risk caps** — max position fraction, daily-loss halt, bankroll cap, global kill switch; a `check(trade, state) → allow/deny+reason`.

## Scoring — the honesty mechanism
A book graduates toward real money only if it is **both** profitable after fees **and** well-calibrated (Brier score beating the market-price baseline). P&L alone over weeks is too noisy to trust.

## Defaults (configurable in `config.yaml`)
- Bankroll **$1,000 notional / book** · max position **5%** · daily-loss halt **10%** · global kill switch
- Evaluation cadence **30 min** over a configurable watchlist
- Models via OpenRouter (Sonnet/Haiku high-frequency, Opus for hard analyst calls)
- `COUNCIL_MODE=paper` (live execution seam built but disabled)

## Engine
Direct **LiteLLM** (CrewAI can't install on Python 3.14). Mock analyst for offline tests.

## Phasing
1. **Trading core** (this commit): market adapter + mock, paper ledger + fees + scoring, risk caps. Fully tested offline.
2. **Strategy books**: the three analyst variants on the council pipeline (mock + LiteLLM).
3. **Dashboard re-aim**: per-book leaderboards, proposed trades, P&L, approval gates.
4. **Live seam**: real Kalshi execution behind `COUNCIL_MODE=live` + hard caps + per-order human approval.

## Deferred (YAGNI)
Real money, multi-user, automated bankroll rebalancing, options beyond binary contracts.
