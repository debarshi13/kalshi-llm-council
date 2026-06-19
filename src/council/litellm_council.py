"""Live council engine via direct LiteLLM calls — no CrewAI.

Works on Python 3.14, where CrewAI cannot install. Same pipeline shape as
MockCouncil, but each role turn is a real completion through ModelClient (which
enforces the per-run budget cap and tracks usage). The architect lays down an
acceptance test (check.py); the implementer writes solution.py; the reviewer
runs the test and gates approval.
"""
from __future__ import annotations

import asyncio
import re

from .council import RunContext, RunResult, RunStatus
from .events import Event, EventType, message, usage
from .models import Role
from .tools import shell
from .tools.workspace import Workspace

_CODE_RE = re.compile(r"```(?:[\w+-]*)\n(.*?)```", re.DOTALL)


def _extract_code(text: str) -> str:
    blocks = _CODE_RE.findall(text)
    return blocks[0].strip() if blocks else text.strip()


class LiteLLMCouncil:
    async def run(self, ctx: RunContext) -> RunResult:
        ws = Workspace(ctx.workspace)
        rid = ctx.run_id
        s = ctx.settings

        async def turn(role: Role, user: str) -> str:
            spec = s.spec(role)
            ctx.bus.publish(Event(EventType.ROLE_STARTED, rid, role=str(role)))
            messages = [
                {"role": "system",
                 "content": f"You are the {role.value} on a multi-model engineering council. {spec.goal}"},
                {"role": "user", "content": user},
            ]
            text, u = await asyncio.to_thread(ctx.client.complete, spec, messages)
            ctx.bus.publish(usage(rid, role, u.as_dict()))
            ctx.bus.publish(message(rid, role, text))
            return text

        # --- Architect: spec + acceptance test -----------------------------
        arch = await turn(
            Role.ARCHITECT,
            f"Task: {ctx.task}\nProduce a brief implementation spec, then an acceptance test "
            "as a SINGLE fenced python code block (this becomes check.py) that imports from "
            "`solution`, asserts the required behavior, and prints OK on success.",
        )
        ws.write_file("check.py", _extract_code(arch))
        ctx.bus.publish(Event(EventType.TOOL_CALL, rid, role=str(Role.ARCHITECT),
                              data={"tool": "workspace.write_file", "path": "check.py"}))

        rounds = 0
        status = RunStatus.MAX_ROUNDS
        final = ""
        last_review = ""

        for attempt in range(s.pipeline.max_revise_rounds + 1):
            rounds = attempt + 1
            ctx.bus.publish(Event(EventType.ROUND_STARTED, rid, data={"round": rounds}))

            # --- Implementer -------------------------------------------------
            impl = await turn(
                Role.IMPLEMENTER,
                f"Spec:\n{arch}\n"
                + (f"\nReviewer feedback to address:\n{last_review}\n" if last_review else "")
                + "\nReturn the FULL contents of solution.py in one fenced code block.",
            )
            code = _extract_code(impl)
            ws.write_file("solution.py", code)
            ctx.bus.publish(Event(EventType.TOOL_CALL, rid, role=str(Role.IMPLEMENTER),
                                  data={"tool": "workspace.write_file", "path": "solution.py"}))

            # --- Reviewer ----------------------------------------------------
            tests_ok = True
            test_block = ""
            if s.pipeline.run_tests:
                granted = await ctx.approval.request(
                    "run_tests", "python3 check.py in the run workspace"
                )
                if not granted:
                    ctx.bus.publish(message(rid, Role.REVIEWER, "Execution denied; aborting."))
                    return self._result(ctx, RunStatus.ABORTED, rounds, "aborted: approval denied")
                ctx.bus.publish(Event(EventType.TOOL_CALL, rid, role=str(Role.REVIEWER),
                                      data={"tool": "shell.run_command", "command": "python3 check.py"}))
                res = await asyncio.to_thread(shell.run_command, "python3 check.py", ctx.workspace, 60)
                ctx.bus.publish(Event(EventType.TEST_RESULT, rid, role=str(Role.REVIEWER),
                                      data=res.to_dict()))
                tests_ok = res.ok
                test_block = f"\nTest exit={res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"

            review = await turn(
                Role.REVIEWER,
                f"Spec:\n{arch}\n\nImplementation:\n{code}\n{test_block}\n\n"
                "Reply APPROVE on the first line if tests pass and the code meets the spec; "
                "otherwise reply REVISE on the first line with concrete fixes.",
            )
            last_review = review
            if tests_ok and review.strip().upper().startswith("APPROVE"):
                status = RunStatus.APPROVED
                final = "Approved by reviewer."
                break

        if status == RunStatus.MAX_ROUNDS:
            final = f"Not approved within {s.pipeline.max_revise_rounds} revise round(s)."
        return self._result(ctx, status, rounds, final)

    @staticmethod
    def _result(ctx: RunContext, status: RunStatus, rounds: int, final: str) -> RunResult:
        return RunResult(
            run_id=ctx.run_id, status=status, rounds=rounds, final_output=final,
            usage=ctx.client.total, workspace=str(ctx.workspace),
            artifacts_dir=str(ctx.artifacts_dir),
        )
