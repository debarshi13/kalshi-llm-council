from conftest import make_settings

from council.artifacts import RunStore
from council.events import EventBus
from council.runner import execute_run


async def test_run_is_logged_to_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki" / "sources").mkdir(parents=True)
    s = make_settings(obsidian={
        "enabled": True, "vault": str(vault), "runs_subdir": "wiki/sources",
    })
    store = RunStore(base_dir=tmp_path / "runs", db_path=tmp_path / "council.db")
    bus = EventBus()

    result = await execute_run("add ints", s, bus, store)

    page = vault / "wiki" / "sources" / f"Council Run {result.run_id}.md"
    assert page.exists()
    text = page.read_text()
    assert "type: source" in text
    assert "add ints" in text
    assert result.status.value in text
    assert "[[Multi-Model AI Council]]" in text
