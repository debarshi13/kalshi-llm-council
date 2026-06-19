import time

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient

from council.api.app import create_app
from council.artifacts import RunStore


@pytest.fixture
def client(tmp_path):
    store = RunStore(base_dir=tmp_path / "runs", db_path=tmp_path / "council.db")
    app = create_app(settings=make_settings(), store=store)
    with TestClient(app) as c:
        yield c


def _wait_for_finish(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = client.get(f"/api/runs/{run_id}").json()
        if row["status"] not in (None, "running"):
            return row
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


def test_start_run_then_completes(client):
    r = client.post("/api/runs", json={"task": "add two ints"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    row = _wait_for_finish(client, run_id)
    assert row["status"] == "approved"
    assert row["result"]["rounds"] == 1


def test_empty_task_rejected(client):
    assert client.post("/api/runs", json={"task": "   "}).status_code == 400


def test_run_list_and_detail(client):
    run_id = client.post("/api/runs", json={"task": "t"}).json()["run_id"]
    _wait_for_finish(client, run_id)
    runs = client.get("/api/runs").json()["runs"]
    assert any(r["run_id"] == run_id for r in runs)
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_event_stream_replays_finished_run(client):
    run_id = client.post("/api/runs", json={"task": "t"}).json()["run_id"]
    _wait_for_finish(client, run_id)
    # After completion the SSE stream replays history and closes.
    body = client.get(f"/api/runs/{run_id}/events").text
    assert "run_started" in body
    assert "run_finished" in body
