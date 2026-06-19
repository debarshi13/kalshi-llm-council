"""The Council interface, run context, results, and the human-approval gate."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from .config import Settings
from .events import Event, EventBus, EventType
from .models import ModelClient, Usage


class RunStatus(str, Enum):
    APPROVED = "approved"        # reviewer accepted
    MAX_ROUNDS = "max_rounds"    # ran out of revise rounds without approval
    ABORTED = "aborted"          # budget / approval denied / fatal
    ERROR = "error"


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    rounds: int
    final_output: str
    usage: Usage
    workspace: str
    artifacts_dir: str

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "rounds": self.rounds,
            "final_output": self.final_output,
            "usage": self.usage.as_dict(),
            "workspace": self.workspace,
            "artifacts_dir": self.artifacts_dir,
        }


class ApprovalDenied(RuntimeError):
    """Raised when a human rejects a gated action."""


class ApprovalGate:
    """Awaitable gate the council uses to pause for human sign-off.

    The council calls ``await request(...)``; the API resolves it via
    ``resolve(approval_id, granted)``. When approval is not required the gate
    auto-grants, so the same code path works headless.
    """

    def __init__(self, bus: EventBus, run_id: str, required: bool) -> None:
        self._bus = bus
        self._run_id = run_id
        self._required = required
        self._pending: dict[str, asyncio.Future] = {}
        self._counter = 0

    async def request(self, action: str, detail: str) -> bool:
        if not self._required:
            return True
        self._counter += 1
        approval_id = f"{self._run_id}-ap{self._counter}"
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[approval_id] = fut
        self._bus.publish(Event(
            EventType.APPROVAL_REQUESTED, self._run_id,
            data={"approval_id": approval_id, "action": action, "detail": detail},
        ))
        granted = await fut
        self._bus.publish(Event(
            EventType.APPROVAL_RESOLVED, self._run_id,
            data={"approval_id": approval_id, "granted": granted},
        ))
        return granted

    def resolve(self, approval_id: str, granted: bool) -> bool:
        fut = self._pending.pop(approval_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(granted)
        return True

    @property
    def pending_ids(self) -> list[str]:
        return list(self._pending)


@dataclass
class RunContext:
    """Everything a council run needs, wired by the runner."""

    run_id: str
    task: str
    settings: Settings
    bus: EventBus
    client: ModelClient
    workspace: Path
    artifacts_dir: Path
    approval: ApprovalGate

    def emit(self, event: Event) -> None:
        self._stamp_and_publish(event)

    def _stamp_and_publish(self, event: Event) -> None:
        self.bus.publish(event)


class Council(Protocol):
    """A council engine runs a coding task and emits events as it goes."""

    async def run(self, ctx: RunContext) -> RunResult: ...
