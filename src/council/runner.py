"""Wire up and execute a single council run end to end."""
from __future__ import annotations

from .artifacts import RunStore, new_run_id
from .config import Settings
from .council import ApprovalGate, Council, RunContext, RunResult, RunStatus
from .events import Event, EventBus, EventType
from .models import ModelClient


def build_engine(name: str) -> Council:
    """Instantiate the orchestration engine by name (lazy imports)."""
    if name == "mock":
        from .mock_council import MockCouncil

        return MockCouncil()
    if name == "crewai":
        from .crew_council import CrewAICouncil

        return CrewAICouncil()
    raise ValueError(f"unknown engine {name!r}")


async def execute_run(
    task: str,
    settings: Settings,
    bus: EventBus,
    store: RunStore,
    run_id: str | None = None,
    engine_override: str | None = None,
    approval: ApprovalGate | None = None,
) -> RunResult:
    run_id = run_id or new_run_id()
    store.create(run_id, task)
    workspace = store.workspace_dir(run_id)
    client = ModelClient(budget_usd=settings.budget_usd)
    # The API path supplies its own gate (so it can resolve approvals from the
    # browser); the CLI/test path lets us create one here.
    if approval is None:
        approval = ApprovalGate(bus, run_id, required=settings.require_approval)
    ctx = RunContext(
        run_id=run_id,
        task=task,
        settings=settings,
        bus=bus,
        client=client,
        workspace=workspace,
        artifacts_dir=store.run_dir(run_id),
        approval=approval,
    )

    engine_name = engine_override or settings.engine
    bus.publish(Event(EventType.RUN_STARTED, run_id, data={"task": task, "engine": engine_name}))

    try:
        council = build_engine(engine_name)
        result = await council.run(ctx)
    except Exception as exc:  # turn any fatal error into a finished-with-error result
        bus.publish(Event(EventType.ERROR, run_id, data={"error": str(exc)}))
        result = RunResult(
            run_id=run_id,
            status=RunStatus.ERROR,
            rounds=0,
            final_output=f"{type(exc).__name__}: {exc}",
            usage=client.total,
            workspace=str(workspace),
            artifacts_dir=str(store.run_dir(run_id)),
        )

    bus.publish(Event(EventType.RUN_FINISHED, run_id, data=result.to_dict()))
    store.save_transcript(run_id, bus.history(run_id))
    store.finalize(result)

    if settings.obsidian.enabled:
        try:
            from .obsidian import log_run

            log_run(settings, result, task)
        except Exception as exc:  # logging is best-effort, never fails a run
            bus.publish(Event(EventType.ERROR, run_id, data={"error": f"obsidian log failed: {exc}"}))

    return result
