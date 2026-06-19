import pytest

from council.events import Event, EventBus, EventType


def test_seq_is_monotonic_per_run():
    bus = EventBus()
    bus.publish(Event(EventType.MESSAGE, "r1"))
    bus.publish(Event(EventType.MESSAGE, "r1"))
    bus.publish(Event(EventType.MESSAGE, "r2"))
    seqs = [e.seq for e in bus.history("r1")]
    assert seqs == [0, 1]
    assert bus.history("r2")[0].seq == 0


async def test_subscribe_replays_then_terminates_on_finish():
    bus = EventBus()
    bus.publish(Event(EventType.RUN_STARTED, "r1"))
    bus.publish(Event(EventType.MESSAGE, "r1", data={"text": "hi"}))
    bus.publish(Event(EventType.RUN_FINISHED, "r1"))

    seen = [ev.type async for ev in bus.subscribe("r1")]
    assert seen == [EventType.RUN_STARTED, EventType.MESSAGE, EventType.RUN_FINISHED]


async def test_error_event_is_not_terminal():
    bus = EventBus()
    bus.publish(Event(EventType.ERROR, "r1", data={"error": "x"}))
    bus.publish(Event(EventType.RUN_FINISHED, "r1"))
    seen = [ev.type async for ev in bus.subscribe("r1")]
    assert seen == [EventType.ERROR, EventType.RUN_FINISHED]
