import os
import sys
from pathlib import Path

import pytest

# Set before ANY module imports / load_env() can leak the real JOURNAL_PATH from .env.
# Keeps the whole test session off the on-disk journal + vault.
os.environ.setdefault("JOURNAL_PATH", ":memory:")
os.environ.pop("COUNCIL_VAULT", None)

# Ensure src/ is importable without an editable install.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from council.config import Settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_journal(monkeypatch):
    """Keep tests off the real journal file / vault: load_settings() reads .env via
    setdefault, which would leak JOURNAL_PATH/COUNCIL_VAULT and make FloorState journals
    share one on-disk DB across tests. Force in-memory + no mirror for every test."""
    monkeypatch.delenv("JOURNAL_PATH", raising=False)
    monkeypatch.delenv("COUNCIL_VAULT", raising=False)
    monkeypatch.setenv("JOURNAL_PATH", ":memory:")


def make_settings(**overrides) -> Settings:
    base = {
        "engine": "mock",
        "roles": {
            "architect": {"model": "mock/architect", "goal": "design"},
            "implementer": {"model": "mock/implementer", "goal": "build"},
            "reviewer": {"model": "mock/reviewer", "goal": "review"},
        },
        "pipeline": {"max_revise_rounds": 2, "run_tests": True},
        "obsidian": {"enabled": False},
        "budget_usd": 5.0,
        "require_approval": False,
    }
    base.update(overrides)
    return Settings.model_validate(base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()
