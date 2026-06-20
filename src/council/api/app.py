"""FastAPI mission-control backend.

Serves the floor game (/) and the data dashboard (/dashboard), exposes a live
paper-floor state the UI polls (/api/floor), and keeps the original council
run endpoints (/api/runs...). A background task ticks the floor so the UI shows
real, evolving activity to monitor and approve.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
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
from ..trading.floor import FloorState

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


class StartRun(BaseModel):
    task: str
    engine: str | None = None


class Approve(BaseModel):
    approval_id: str
    granted: bool = True


class FloorApprove(BaseModel):
    id: int
    granted: bool = True


class FloorStop(BaseModel):
    frozen: bool


class FloorToggle(BaseModel):
    on: bool


def _page(name: str) -> FileResponse:
    p = WEB_DIR / name
    if not p.exists():
        raise HTTPException(404, f"{name} not found in web/")
    return FileResponse(p)


def create_app(settings: Settings | None = None, store: RunStore | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def floor_loop():
            while True:
                await asyncio.sleep(3)
                try:
                    app.state.floor.tick()
                except Exception:  # never let the monitor loop crash the server
                    pass
        task = asyncio.create_task(floor_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Council Mission Control", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings or load_settings()
    app.state.bus = EventBus()
    app.state.store = store or RunStore()
    app.state.gates = {}
    app.state.tasks = {}
    app.state.floor = FloorState()

    # ── live floor (what the UI polls) ──────────────────────────────────────
    @app.get("/api/floor")
    async def floor():
        return app.state.floor.snapshot()

    @app.post("/api/floor/approve")
    async def floor_approve(body: FloorApprove):
        if not app.state.floor.approve(body.id, body.granted):
            raise HTTPException(404, "no such ticket")
        return {"ok": True}

    @app.post("/api/floor/stop")
    async def floor_stop(body: FloorStop):
        app.state.floor.frozen = body.frozen
        return {"frozen": body.frozen}

    @app.post("/api/floor/auto")
    async def floor_auto(body: FloorToggle):
        app.state.floor.auto = body.on
        return {"auto": body.on}

    @app.post("/api/floor/live")
    async def floor_live(body: FloorToggle):
        """Arm/disarm the REAL models. Turning this on starts spending tokens."""
        floor = app.state.floor
        if body.on:
            floor.enable_live(budget=app.state.settings.budget_usd)
        else:
            floor.live = False
        return {"live": floor.live}

    # ── council runs (original) ─────────────────────────────────────────────
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

    # ── pages ───────────────────────────────────────────────────────────────
    @app.get("/")
    async def game():
        return _page("floor-game.html")

    @app.get("/dashboard")
    async def dashboard():
        return _page("mission-control.html")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    return app


app = create_app()
