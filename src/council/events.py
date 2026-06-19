"""Typed events + an async pub/sub bus with per-run replay.

The council emits :class:`Event`s; the FastAPI layer relays them to the
dashboard over SSE. Late subscribers (a browser tab opened mid-run) get the
full history replayed first, then live events.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import AsyncIterator


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    ROUND_STARTED = "round_started"
    ROLE_STARTED = "role_started"
    MESSAGE = "message"            # a model's textual output
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TEST_RESULT = "test_result"
    USAGE = "usage"               # token/cost delta
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    RUN_FINISHED = "run_finished"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    run_id: str
    role: str | None = None
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0  # assigned by the bus, monotonic per run

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d


class EventBus:
    """In-process async pub/sub keyed by run_id, with bounded history."""

    def __init__(self, history_limit: int = 2000) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._history: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}
        self._history_limit = history_limit

    def publish(self, event: Event) -> None:
        event.seq = self._seq.get(event.run_id, 0)
        self._seq[event.run_id] = event.seq + 1

        hist = self._history.setdefault(event.run_id, [])
        hist.append(event)
        if len(hist) > self._history_limit:
            del hist[: len(hist) - self._history_limit]

        for q in list(self._subscribers.get(event.run_id, ())):
            q.put_nowait(event)

    def history(self, run_id: str) -> list[Event]:
        return list(self._history.get(run_id, ()))

    async def subscribe(self, run_id: str, replay: bool = True) -> AsyncIterator[Event]:
        """Yield events for a run. Replays history first, then streams live.

        Terminates after the RUN_FINISHED event is delivered. ERROR events are
        informational; the runner always closes a run with RUN_FINISHED (with an
        error status if it failed), so that is the single terminal signal.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(q)
        try:
            if replay:
                for ev in self.history(run_id):
                    yield ev
                    if ev.type is EventType.RUN_FINISHED:
                        return
            while True:
                ev = await q.get()
                yield ev
                if ev.type is EventType.RUN_FINISHED:
                    return
        finally:
            subs = self._subscribers.get(run_id)
            if subs:
                subs.discard(q)


# Convenience constructors keep call sites terse and consistent.
def message(run_id: str, role, text: str, **extra) -> Event:
    return Event(EventType.MESSAGE, run_id, role=str(role) if role else None,
                 data={"text": text, **extra})


def usage(run_id: str, role, usage_dict: dict) -> Event:
    return Event(EventType.USAGE, run_id, role=str(role) if role else None, data=usage_dict)
