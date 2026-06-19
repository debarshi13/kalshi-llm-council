# Multi-Model Coding Council — Design Spec

**Date:** 2026-06-19
**Status:** Approved (brainstorming), implementation in progress
**Related vault page:** `The Brain/wiki/sources/Multi-Model Council Architecture Decision.md`

## Purpose
A coding assistant where three diverse LLMs — **Claude**, **Kimi K2**, **GLM-5.2** — collaborate on a technical task under **fixed roles**, driven and observed from a **local web mission-control dashboard**.

## Decisions (locked in brainstorming)
- **Primary use:** coding / technical work (tool access + verification matter).
- **Collaboration:** fixed role specialization (architect → implementer → reviewer/tester).
- **UI:** local web dashboard.
- **Framework:** CrewAI (role/goal/task/crew maps 1:1 onto fixed roles).
- **Model gateway:** LiteLLM, so any model (or a self-hosted endpoint) plugs in via config only.
- **Decided earlier (vault):** hosted APIs first; GPU self-hosting deferred. LiteLLM is the seam that keeps that swap config-only.

## Role → model mapping (default, config-driven)
| Role | Model | Rationale |
|---|---|---|
| Architect | Claude | strongest planning/tool-use; writes the spec |
| Implementer | Kimi K2 | strong, cheap coder |
| Reviewer/Tester | GLM-5.2 | 1M context; different model than implementer = uncorrelated review |

**Invariant:** implementer ≠ reviewer model.

## Architecture (layered, engine behind an interface)
```
                +------------------ web dashboard (web/) ------------------+
                |  role lanes · live messages · pipeline · $ meter · gates |
                +----------------------------+-----------------------------+
                                             | SSE
                +----------------------------v-----------------------------+
                |                    FastAPI app (api/)                     |
                |        run management · /events SSE · approvals          |
                +----------------------------+-----------------------------+
                                             |
        +------------------------------------v-------------------------------------+
        |                        Council interface (council.py)                    |
        |        run(task) -> emits Events on the async EventBus (events.py)        |
        +------------------+-----------------------------------+-------------------+
                           |                                   |
              +------------v-----------+         +-------------v--------------+
              |  CrewAICouncil         |         |  MockCouncil               |
              |  (crew_council.py)     |         |  (offline, no API keys)    |
              |  real models via       |         |  deterministic scripted    |
              |  LiteLLM               |         |  pipeline for dev/tests    |
              +------------+-----------+         +----------------------------+
                           |
        +------------------v-----------------------------------+
        |  Tools: workspace (file r/w, confined) · shell/test  |
        |  runner (subprocess, timeout, approval-gated)        |
        +------------------------------------------------------+
                           |
        +------------------v-----------------------------------+
        |  Artifacts (artifacts.py): transcript, diffs,        |
        |  token/cost, SQLite history · Obsidian log           |
        +------------------------------------------------------+
```

## Components
- **config.py** — load `config.yaml` + env; pydantic-validated; enforces implementer ≠ reviewer.
- **models.py** — `Role` enum; `ModelSpec`; LiteLLM-backed `complete()` (lazy import) + cost accounting.
- **events.py** — typed `Event`s + async `EventBus` (pub/sub) the API streams over SSE.
- **council.py** — `Council` protocol: `async run(task, ctx) -> RunResult`, emitting events.
- **mock_council.py** — deterministic scripted architect→implement→review run; no network. Powers tests + offline demo.
- **crew_council.py** — CrewAI implementation; subscribes CrewAI events → our EventBus.
- **tools/workspace.py** — read/write files confined to a per-run workspace dir (path-escape guarded).
- **tools/shell.py** — run commands/tests in the workspace; timeout; approval gate; output capped.
- **artifacts.py** — persist run transcript/diffs/usage to `runs/<id>/` + SQLite index.
- **obsidian.py** — append a run summary as a wiki source page (filesystem transport).
- **api/app.py**, **api/routes.py** — FastAPI: `POST /runs`, `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/events` (SSE), `POST /runs/{id}/approve`.
- **web/** — static dashboard (vanilla JS + SSE).
- **cli.py** — `council run "<task>"` end-to-end in the terminal.

## Pipeline
1. **Architect** turns the task into a minimal implementation spec.
2. **Implementer** writes code into the run workspace.
3. **Reviewer** critiques + (if `run_tests`) invokes the test runner.
4. If reviewer rejects and rounds remain (`max_revise_rounds`), implementer revises → back to 3.
5. Terminate on reviewer approval or round cap; emit `RunFinished`.

## Safety
- All file/exec confined to `runs/<id>/workspace/`.
- Shell/test exec and write-back are **approval-gated** when `COUNCIL_REQUIRE_APPROVAL=true` (dashboard button).
- Per-run USD budget cap; run aborts if projected spend exceeds it.
- No Docker on host yet → subprocess sandbox with timeout + output caps; Docker executor is a future hardening step.

## Testing
Pytest over config validation, event bus, workspace path-escape guards, the full MockCouncil pipeline, artifact persistence, and API routes (via the mock engine). All green without API keys.

## Deferred (YAGNI for now)
Self-hosted GLM-5.2 endpoint, multi-user, auth, vector memory, parallel/debate topologies. The LiteLLM seam + Council interface keep these cheap to add later.
