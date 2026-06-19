"""Offline council engine: a real pipeline with scripted model turns.

It exercises every moving part for real — workspace writes, approval gates,
sandboxed test execution, event emission, the revise loop — but substitutes
deterministic canned text for live model output. This powers the test suite and
an offline ``engine: mock`` demo, with zero API keys.
"""
from __future__ import annotations

import asyncio

from .council import RunContext, RunResult, RunStatus
from .events import Event, EventType, message, usage
from .models import Role, Usage
from .tools import shell
from .tools.workspace import Workspace

# Acceptance test the "architect" lays down; the implementer must satisfy it.
_CHECK = """from solution import add
assert add(2, 3) == 5, f"expected 5, got {add(2, 3)}"
print("OK")
"""
_GOOD = "def add(a, b):\n    return a + b\n"
_BUGGY = "def add(a, b):\n    return a - b  # deliberate bug\n"


class MockCouncil:
    """Deterministic architect -> implementer -> reviewer pipeline.

    :param fail_first: number of review rounds to fail before producing correct
        code. ``0`` passes on the first round; ``1`` exercises one revise loop.
    """

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first

    async def run(self, ctx: RunContext) -> RunResult:
        ws = Workspace(ctx.workspace)
        rid = ctx.run_id

        def emit(ev: Event) -> None:
            ctx.bus.publish(ev)

        def fake_usage(role: Role, pt: int, ct: int) -> None:
            u = Usage(pt, ct, 0.0)
            ctx.client.total = ctx.client.total + u
            emit(usage(rid, role, u.as_dict()))

        # --- Architect: define the approach + acceptance test ---------------
        emit(Event(EventType.ROLE_STARTED, rid, role=str(Role.ARCHITECT)))
        emit(message(rid, Role.ARCHITECT,
                     f"Spec for task: {ctx.task!r}. Provide solution.py exposing add(a, b); "
                     "acceptance test check.py must print OK."))
        ws.write_file("check.py", _CHECK)
        emit(Event(EventType.TOOL_CALL, rid, role=str(Role.ARCHITECT),
                   data={"tool": "workspace.write_file", "path": "check.py"}))
        fake_usage(Role.ARCHITECT, 120, 60)

        rounds = 0
        status = RunStatus.MAX_ROUNDS
        final = ""
        max_rounds = ctx.settings.pipeline.max_revise_rounds

        for attempt in range(max_rounds + 1):
            rounds = attempt + 1
            emit(Event(EventType.ROUND_STARTED, rid, data={"round": rounds}))

            # --- Implementer ------------------------------------------------
            emit(Event(EventType.ROLE_STARTED, rid, role=str(Role.IMPLEMENTER)))
            buggy = attempt < self.fail_first
            ws.write_file("solution.py", _BUGGY if buggy else _GOOD)
            emit(message(rid, Role.IMPLEMENTER,
                         ("Revised solution." if attempt else "Initial solution.")
                         + " Wrote solution.py."))
            emit(Event(EventType.TOOL_CALL, rid, role=str(Role.IMPLEMENTER),
                       data={"tool": "workspace.write_file", "path": "solution.py"}))
            fake_usage(Role.IMPLEMENTER, 200, 90)

            # --- Reviewer ---------------------------------------------------
            emit(Event(EventType.ROLE_STARTED, rid, role=str(Role.REVIEWER)))
            if ctx.settings.pipeline.run_tests:
                granted = await ctx.approval.request(
                    "run_tests", "python3 check.py in the run workspace"
                )
                if not granted:
                    emit(message(rid, Role.REVIEWER, "Test execution denied by operator; aborting."))
                    status = RunStatus.ABORTED
                    final = "aborted: approval denied"
                    break
                emit(Event(EventType.TOOL_CALL, rid, role=str(Role.REVIEWER),
                           data={"tool": "shell.run_command", "command": "python3 check.py"}))
                res = await asyncio.to_thread(shell.run_command, "python3 check.py", ctx.workspace, 30)
                emit(Event(EventType.TEST_RESULT, rid, role=str(Role.REVIEWER), data=res.to_dict()))
                passed = res.ok
            else:
                passed = not buggy

            fake_usage(Role.REVIEWER, 180, 70)

            if passed:
                emit(message(rid, Role.REVIEWER, "Tests pass. Approved."))
                status = RunStatus.APPROVED
                final = "Approved. solution.py satisfies check.py."
                break
            emit(message(rid, Role.REVIEWER,
                         f"Tests fail (round {rounds}). Requesting revision."))

        if status == RunStatus.MAX_ROUNDS:
            final = f"Not approved within {max_rounds} revise round(s)."

        return RunResult(
            run_id=rid,
            status=status,
            rounds=rounds,
            final_output=final,
            usage=ctx.client.total,
            workspace=str(ctx.workspace),
            artifacts_dir=str(ctx.artifacts_dir),
        )
