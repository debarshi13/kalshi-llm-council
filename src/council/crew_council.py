"""Live council engine backed by CrewAI + LiteLLM.

Design note: CrewAI's sequential ``Process`` can't natively express a
"revise until tests pass, with human approval gates mid-flow" loop. So this
engine uses CrewAI for what it's good at — constructing a role-specialized
``Agent`` and running its ``Task`` against the configured model — while *our*
loop owns control flow, tool execution, approval gates, and event streaming.
That keeps the live path behaviourally identical to MockCouncil.

Only exercised with the ``[real]`` extra installed and API keys present;
imports are lazy so the rest of the framework never depends on CrewAI.
"""
from __future__ import annotations

import asyncio
import re

from .council import RunContext, RunResult, RunStatus
from .events import Event, EventType, message, usage
from .models import BudgetExceeded, ModelSpec, Role, Usage
from .tools import shell
from .tools.workspace import Workspace

_CODE_RE = re.compile(r"```(?:[\w+-]*)\n(.*?)```", re.DOTALL)


def _extract_code(text: str) -> str:
    blocks = _CODE_RE.findall(text)
    return blocks[0].strip() if blocks else text.strip()


class CrewAICouncil:
    async def run(self, ctx: RunContext) -> RunResult:
        ws = Workspace(ctx.workspace)
        rid = ctx.run_id
        s = ctx.settings

        async def turn(role: Role, description: str, expected: str) -> str:
            ctx.bus.publish(Event(EventType.ROLE_STARTED, rid, role=str(role)))
            text, u = await asyncio.to_thread(
                self._agent_turn, s.spec(role), role, description, expected
            )
            ctx.client.total = ctx.client.total + u
            ctx.bus.publish(usage(rid, role, u.as_dict()))
            ctx.bus.publish(message(rid, role, text))
            if ctx.client.total.cost_usd >= ctx.client.budget_usd:
                raise BudgetExceeded(
                    f"run spend ${ctx.client.total.cost_usd:.4f} reached cap "
                    f"${ctx.client.budget_usd:.2f}"
                )
            return text

        # --- Architect -------------------------------------------------------
        spec_text = await turn(
            Role.ARCHITECT,
            f"You are the architect. Task: {ctx.task}\n"
            "Produce a concise implementation spec: files to create, the public "
            "interface, and acceptance criteria. Do not write the full implementation.",
            "A short implementation spec.",
        )

        rounds = 0
        status = RunStatus.MAX_ROUNDS
        final = ""
        last_review = ""

        for attempt in range(s.pipeline.max_revise_rounds + 1):
            rounds = attempt + 1
            ctx.bus.publish(Event(EventType.ROUND_STARTED, rid, data={"round": rounds}))

            # --- Implementer -------------------------------------------------
            impl_desc = (
                f"You are the implementer. Spec:\n{spec_text}\n\n"
                + (f"Reviewer feedback to address:\n{last_review}\n\n" if last_review else "")
                + "Return the FULL contents of solution.py in a single fenced code block."
            )
            impl_text = await turn(Role.IMPLEMENTER, impl_desc, "solution.py in a code block.")
            code = _extract_code(impl_text)
            ws.write_file("solution.py", code)
            ctx.bus.publish(Event(EventType.TOOL_CALL, rid, role=str(Role.IMPLEMENTER),
                                  data={"tool": "workspace.write_file", "path": "solution.py"}))

            # --- Reviewer ----------------------------------------------------
            test_block = ""
            if s.pipeline.run_tests:
                granted = await ctx.approval.request(
                    "run_tests", "execute the project's tests in the run workspace"
                )
                if not granted:
                    ctx.bus.publish(message(rid, Role.REVIEWER, "Execution denied; aborting."))
                    return self._result(ctx, RunStatus.ABORTED, rounds, "aborted: approval denied")
                cmd = "python3 -m pytest -q 2>/dev/null || python3 check.py"
                ctx.bus.publish(Event(EventType.TOOL_CALL, rid, role=str(Role.REVIEWER),
                                      data={"tool": "shell.run_command", "command": cmd}))
                res = await asyncio.to_thread(shell.run_command, cmd, ctx.workspace, 60)
                ctx.bus.publish(Event(EventType.TEST_RESULT, rid, role=str(Role.REVIEWER),
                                      data=res.to_dict()))
                test_block = f"\nTest command: {cmd}\nexit={res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
                tests_ok = res.ok
            else:
                tests_ok = True

            review = await turn(
                Role.REVIEWER,
                f"You are the reviewer. Spec:\n{spec_text}\n\nImplementation:\n{code}\n"
                f"{test_block}\n\n"
                "If tests pass and the code meets the spec, reply with APPROVE on the first line. "
                "Otherwise reply REVISE on the first line and list concrete fixes.",
                "APPROVE or REVISE with reasons.",
            )
            last_review = review
            approved = review.strip().upper().startswith("APPROVE") and tests_ok
            if approved:
                status = RunStatus.APPROVED
                final = "Approved by reviewer."
                break

        if status == RunStatus.MAX_ROUNDS:
            final = f"Not approved within {s.pipeline.max_revise_rounds} revise round(s)."
        return self._result(ctx, status, rounds, final)

    # --- helpers ------------------------------------------------------------
    def _result(self, ctx: RunContext, status: RunStatus, rounds: int, final: str) -> RunResult:
        return RunResult(
            run_id=ctx.run_id,
            status=status,
            rounds=rounds,
            final_output=final,
            usage=ctx.client.total,
            workspace=str(ctx.workspace),
            artifacts_dir=str(ctx.artifacts_dir),
        )

    @staticmethod
    def _agent_turn(spec: ModelSpec, role: Role, description: str, expected: str) -> tuple[str, Usage]:
        """Run one CrewAI Agent+Task against the role's model. Live-only."""
        from crewai import Agent, Crew, Task, LLM, Process

        llm = LLM(model=spec.model, temperature=spec.temperature)
        agent = Agent(
            role=role.value,
            goal=spec.goal,
            backstory=f"A specialist {role.value} on a multi-model engineering council.",
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )
        task = Task(description=description, expected_output=expected, agent=agent)
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        result = crew.kickoff()
        text = str(getattr(result, "raw", result))

        u = Usage()
        metrics = getattr(crew, "usage_metrics", None)
        if metrics is not None:
            pt = int(getattr(metrics, "prompt_tokens", 0) or 0)
            ct = int(getattr(metrics, "completion_tokens", 0) or 0)
            u = Usage(pt, ct, 0.0)  # CrewAI doesn't surface $ directly; left to cost dashboards
        return text, u
