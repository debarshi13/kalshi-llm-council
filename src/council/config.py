"""Configuration: load ``config.yaml`` + environment, validate, expose typed settings."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from .models import ModelSpec, Role


class RoleConfig(BaseModel):
    model: str
    goal: str
    temperature: float = 0.2

    def to_spec(self) -> ModelSpec:
        return ModelSpec(model=self.model, goal=self.goal, temperature=self.temperature)


class PipelineConfig(BaseModel):
    max_revise_rounds: int = Field(default=2, ge=0, le=6)
    run_tests: bool = True


class ObsidianConfig(BaseModel):
    enabled: bool = False
    vault: str | None = None
    runs_subdir: str = "wiki/sources"


class Settings(BaseModel):
    engine: str = "crewai"  # "crewai" | "mock"
    roles: dict[Role, RoleConfig]
    pipeline: PipelineConfig = PipelineConfig()
    obsidian: ObsidianConfig = ObsidianConfig()

    # from environment
    budget_usd: float = 2.0
    require_approval: bool = True

    @model_validator(mode="after")
    def _check_roles(self) -> "Settings":
        required = {Role.ARCHITECT, Role.IMPLEMENTER, Role.REVIEWER}
        missing = required - set(self.roles)
        if missing:
            raise ValueError(f"config.yaml missing role(s): {sorted(r.value for r in missing)}")
        # Core invariant: review must be a *different* model than implementation.
        if self.roles[Role.IMPLEMENTER].model == self.roles[Role.REVIEWER].model:
            raise ValueError(
                "implementer and reviewer must use different models "
                "(diverse review is the whole point of a council)"
            )
        if self.engine not in {"litellm", "crewai", "mock"}:
            raise ValueError(f"engine must be 'litellm', 'crewai', or 'mock', got {self.engine!r}")
        return self

    def spec(self, role: Role) -> ModelSpec:
        return self.roles[role].to_spec()


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    """Load and validate settings from YAML + environment."""
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw["budget_usd"] = float(os.environ.get("COUNCIL_BUDGET_USD", raw.get("budget_usd", 2.0)))
    raw["require_approval"] = _env_bool("COUNCIL_REQUIRE_APPROVAL", raw.get("require_approval", True))
    return Settings.model_validate(raw)
