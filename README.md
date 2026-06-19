# Council — a multi-model coding council

Three diverse LLMs collaborate on a coding task under **fixed roles**, driven from a
local **mission-control dashboard**:

| Role | Default model | Job |
|------|---------------|-----|
| **Architect** | Claude | decompose the task, write the spec + acceptance criteria |
| **Implementer** | Kimi K2 | write the smallest code that satisfies the spec |
| **Reviewer / Tester** | GLM-5.2 | critique, run the tests, demand revisions until green |

> Invariant: the implementer and reviewer are always **different models**, so review
> has real (uncorrelated) diversity. Models route through **LiteLLM**, so swapping a
> hosted model for a self-hosted endpoint is a config change, not a rewrite.

## Architecture
The orchestration engine sits behind a `Council` interface with two implementations:
- **`MockCouncil`** — runs the *real* pipeline (real workspace writes, real sandboxed
  test execution, approval gates, the revise loop) with scripted model turns. **No API
  keys needed** — powers the test suite and an offline demo.
- **`CrewAICouncil`** — the live engine; CrewAI builds each role's agent, our loop owns
  control flow, tools, approvals, and event streaming.

Everything emits typed **events** onto an async bus; the FastAPI backend relays them to
the browser over **SSE**. See [`docs/design.md`](docs/design.md) for the full spec.

```
web/ dashboard ──SSE── FastAPI (api/) ── Council interface ──┬─ MockCouncil (offline)
                                                             └─ CrewAICouncil (live)
                                          tools: workspace · sandboxed shell
                                          artifacts: runs/<id>/ + SQLite · Obsidian log
```

## Setup
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # core + test deps
# for live runs (optional; may lag newest Python):
pip install -e ".[real]"
cp .env.example .env             # add OpenRouter key (or per-provider keys)
```

## Run it offline (no keys)
```bash
council run "implement a function that parses ISO-8601 durations" --engine mock
```

## Run the dashboard
```bash
uvicorn council.api.app:app --reload --port 8777
# open http://127.0.0.1:8777
```
Type a task, watch the three role lanes stream live, approve gated test runs, and see
token/cost in the HUD. Run history is in the left rail.

## Go live
1. `pip install -e ".[real]"` and fill `.env`.
2. `python scripts/check_connectivity.py` — confirms all three models respond.
3. Set `engine: crewai` in `config.yaml` (or pass `--engine crewai`).

## Safety
- All file I/O and command execution are confined to `runs/<id>/workspace/`.
- Test/shell execution and write-back are **approval-gated** (`COUNCIL_REQUIRE_APPROVAL=true`).
- Per-run USD budget cap (`COUNCIL_BUDGET_USD`) aborts a runaway loop.
- A destructive-command denylist + timeouts + output caps back the subprocess sandbox.
  (Docker-backed isolation is the planned hardening step.)

## Test
```bash
pytest -q          # 24 tests, all green without API keys
```

## Configuration
Role→model mapping, pipeline rounds, and Obsidian logging live in `config.yaml`.
Secrets and runtime toggles live in `.env`.
