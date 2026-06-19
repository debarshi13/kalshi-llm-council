import pytest
from conftest import make_settings

from council.config import Settings
from council.models import Role


def test_valid_settings_expose_specs():
    s = make_settings()
    assert s.spec(Role.ARCHITECT).model == "mock/architect"
    assert s.pipeline.max_revise_rounds == 2


def test_implementer_and_reviewer_must_differ():
    with pytest.raises(ValueError, match="different models"):
        make_settings(roles={
            "architect": {"model": "a", "goal": "g"},
            "implementer": {"model": "same", "goal": "g"},
            "reviewer": {"model": "same", "goal": "g"},
        })


def test_missing_role_rejected():
    with pytest.raises(ValueError, match="missing role"):
        Settings.model_validate({
            "engine": "mock",
            "roles": {"architect": {"model": "a", "goal": "g"}},
        })


def test_bad_engine_rejected():
    with pytest.raises(ValueError, match="engine must be"):
        make_settings(engine="nope")
