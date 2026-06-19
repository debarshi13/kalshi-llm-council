import sys
from pathlib import Path

import pytest

# Ensure src/ is importable without an editable install.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from council.config import Settings  # noqa: E402


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
