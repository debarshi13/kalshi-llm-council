"""Command-line entrypoint: run a coding task through the council."""
from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.panel import Panel

from .artifacts import RunStore
from .config import load_settings
from .events import EventBus, EventType
from .runner import execute_run

console = Console()

_ROLE_STYLE = {"architect": "cyan", "implementer": "green", "reviewer": "magenta"}


async def _run(task: str, engine: str | None, config: str) -> int:
    settings = load_settings(config)
    bus = EventBus()
    store = RunStore()

    async def render(run_id: str) -> None:
        async for ev in bus.subscribe(run_id):
            style = _ROLE_STYLE.get(ev.role or "", "white")
            if ev.type is EventType.MESSAGE:
                console.print(Panel(ev.data.get("text", ""), title=f"[{style}]{ev.role}",
                                    border_style=style, expand=False))
            elif ev.type is EventType.TEST_RESULT:
                ok = ev.data.get("ok")
                console.print(f"  [bold]{'✓ tests pass' if ok else '✗ tests fail'}[/] "
                              f"(exit {ev.data.get('returncode')})")
            elif ev.type is EventType.ROUND_STARTED:
                console.rule(f"round {ev.data.get('round')}")
            elif ev.type is EventType.RUN_FINISHED:
                d = ev.data
                console.print(Panel(
                    f"status: [bold]{d['status']}[/]\nrounds: {d['rounds']}\n"
                    f"tokens: {d['usage']['total_tokens']}  cost: ${d['usage']['cost_usd']:.4f}\n"
                    f"workspace: {d['workspace']}",
                    title="run finished", border_style="bold"))

    # Pre-create the run id so the renderer and executor share it.
    from .artifacts import new_run_id
    run_id = new_run_id()
    render_task = asyncio.create_task(render(run_id))
    result = await execute_run(task, settings, bus, store, run_id=run_id, engine_override=engine)
    await render_task
    return 0 if result.status.value in {"approved"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="council", description="Run a multi-model coding council.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run a coding task")
    run_p.add_argument("task", help="the task description")
    run_p.add_argument("--engine", choices=["crewai", "mock"], default=None,
                       help="override config engine (use 'mock' for an offline demo)")
    run_p.add_argument("--config", default="config.yaml")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_run(args.task, args.engine, args.config))
    return 2


if __name__ == "__main__":
    sys.exit(main())
