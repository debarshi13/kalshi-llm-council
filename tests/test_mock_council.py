import pytest
from conftest import make_settings

from council.artifacts import RunStore
from council.council import RunStatus
from council.events import EventBus, EventType
from council.runner import execute_run


@pytest.fixture
def store(tmp_path):
    return RunStore(base_dir=tmp_path / "runs", db_path=tmp_path / "council.db")


async def test_happy_path_approves_and_persists(store):
    s = make_settings()
    bus = EventBus()
    result = await execute_run("add two numbers", s, bus, store)

    assert result.status is RunStatus.APPROVED
    assert result.rounds == 1
    # real files were written into the sandbox
    assert (store.workspace_dir(result.run_id) / "solution.py").exists()
    assert (store.workspace_dir(result.run_id) / "check.py").exists()
    # transcript + result + sqlite row persisted
    assert (store.run_dir(result.run_id) / "transcript.json").exists()
    row = store.get_run(result.run_id)
    assert row["status"] == "approved"
    # a real test was executed and reported
    assert any(e.type is EventType.TEST_RESULT and e.data["ok"] for e in bus.history(result.run_id))


async def test_revise_loop_recovers(store):
    # fail the first review round, then pass -> 2 rounds, still approved
    from council.mock_council import MockCouncil
    import council.runner as runner

    orig = runner.build_engine
    runner.build_engine = lambda name: MockCouncil(fail_first=1)
    try:
        s = make_settings()
        bus = EventBus()
        result = await execute_run("add", s, bus, store)
    finally:
        runner.build_engine = orig

    assert result.status is RunStatus.APPROVED
    assert result.rounds == 2


async def test_max_rounds_exhausted(store):
    from council.mock_council import MockCouncil
    import council.runner as runner

    orig = runner.build_engine
    runner.build_engine = lambda name: MockCouncil(fail_first=99)
    try:
        s = make_settings(pipeline={"max_revise_rounds": 1, "run_tests": True})
        bus = EventBus()
        result = await execute_run("add", s, bus, store)
    finally:
        runner.build_engine = orig

    assert result.status is RunStatus.MAX_ROUNDS
    assert result.rounds == 2  # initial + 1 revise


async def test_finishes_with_terminal_event(store):
    s = make_settings()
    bus = EventBus()
    result = await execute_run("x", s, bus, store)
    types = [e.type for e in bus.history(result.run_id)]
    assert types[0] is EventType.RUN_STARTED
    assert types[-1] is EventType.RUN_FINISHED
