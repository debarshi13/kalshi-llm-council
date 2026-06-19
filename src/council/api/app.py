"""FastAPI mission-control backend.

Wraps the council: start runs, stream their events over SSE, browse history,
and resolve human-approval gates from the dashboard.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..artifacts import RunStore, new_run_id
from ..config import Settings, load_settings
from ..council import ApprovalGate
from ..events import EventBus, EventType
from ..runner import execute_run

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


class StartRun(BaseModel):
    task: str
    engine: str | None = None


class Approve(BaseModel):
    approval_id: str
    granted: bool = True


def create_app(settings: Settings | None = None, store: RunStore | None = None) -> FastAPI:
    app = FastAPI(title="Council Mission Control", version="0.1.0")
    app.state.settings = settings or load_settings()
    app.state.bus = EventBus()
    app.state.store = store or RunStore()
    app.state.gates = {}        # run_id -> ApprovalGate
    app.state.tasks = {}        # run_id -> asyncio.Task (keep refs alive)

    @app.post("/api/runs")
    async def start_run(body: StartRun):
        if not body.task.strip():
            raise HTTPException(400, "task must not be empty")
        run_id = new_run_id()
        gate = ApprovalGate(app.state.bus, run_id, required=app.state.settings.require_approval)
        app.state.gates[run_id] = gate

        async def _go():
            try:
                await execute_run(
                    body.task, app.state.settings, app.state.bus, app.state.store,
                    run_id=run_id, engine_override=body.engine, approval=gate,
                )
            finally:
                app.state.gates.pop(run_id, None)

        app.state.tasks[run_id] = asyncio.create_task(_go())
        return {"run_id": run_id}

    @app.get("/api/runs")
    async def list_runs():
        return {"runs": app.state.store.list_runs()}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        row = app.state.store.get_run(run_id)
        if not row:
            raise HTTPException(404, "no such run")
        return row

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str):
        bus: EventBus = app.state.bus

        async def gen():
            async for ev in bus.subscribe(run_id):
                yield {"event": ev.type.value, "data": json.dumps(ev.to_dict())}

        return EventSourceResponse(gen())

    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str, body: Approve):
        gate: ApprovalGate | None = app.state.gates.get(run_id)
        if gate is None:
            raise HTTPException(404, "run not active or has no pending approvals")
        if not gate.resolve(body.approval_id, body.granted):
            raise HTTPException(409, "approval already resolved or unknown")
        return {"ok": True}

    @app.get("/")
    async def index():
        idx = WEB_DIR / "index.html"
        if not idx.exists():
            return {"service": "council", "ui": "missing", "hint": "web/index.html not found"}
        return FileResponse(idx)

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app


app = create_app()
