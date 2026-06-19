import asyncio

import pytest
from conftest import make_settings

from council.artifacts import RunStore
from council.council import ApprovalGate, RunContext, RunStatus
from council.events import EventBus, EventType
from council.mock_council import MockCouncil
from council.models import BudgetExceeded, ModelClient, ModelSpec


def test_budget_blocks_before_any_call():
    client = ModelClient(budget_usd=0.0)
    with pytest.raises(BudgetExceeded):
        client.complete(ModelSpec("m", "g"), [{"role": "user", "content": "hi"}])


async def test_denied_approval_aborts_run(tmp_path):
    s = make_settings(require_approval=True)
    bus = EventBus()
    store = RunStore(base_dir=tmp_path / "runs", db_path=tmp_path / "council.db")
    run_id = "r-deny"
    store.create(run_id, "task")
    gate = ApprovalGate(bus, run_id, required=True)
    ctx = RunContext(
        run_id=run_id, task="task", settings=s, bus=bus, client=ModelClient(5.0),
        workspace=store.workspace_dir(run_id), artifacts_dir=store.run_dir(run_id), approval=gate,
    )

    task = asyncio.create_task(MockCouncil().run(ctx))
    # Wait for the gate to request approval, then deny it.
    for _ in range(200):
        reqs = [e for e in bus.history(run_id) if e.type is EventType.APPROVAL_REQUESTED]
        if reqs:
            break
        await asyncio.sleep(0.01)
    assert reqs, "approval was never requested"
    assert gate.resolve(reqs[0].data["approval_id"], False)

    result = await task
    assert result.status is RunStatus.ABORTED


async def test_granted_approval_completes(tmp_path):
    s = make_settings(require_approval=True)
    bus = EventBus()
    store = RunStore(base_dir=tmp_path / "runs", db_path=tmp_path / "council.db")
    run_id = "r-grant"
    store.create(run_id, "task")
    gate = ApprovalGate(bus, run_id, required=True)
    ctx = RunContext(
        run_id=run_id, task="task", settings=s, bus=bus, client=ModelClient(5.0),
        workspace=store.workspace_dir(run_id), artifacts_dir=store.run_dir(run_id), approval=gate,
    )
    task = asyncio.create_task(MockCouncil().run(ctx))
    for _ in range(200):
        reqs = [e for e in bus.history(run_id) if e.type is EventType.APPROVAL_REQUESTED]
        if reqs:
            break
        await asyncio.sleep(0.01)
    gate.resolve(reqs[0].data["approval_id"], True)
    result = await task
    assert result.status is RunStatus.APPROVED
