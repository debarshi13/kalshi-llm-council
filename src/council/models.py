"""Roles, model specs, and the LiteLLM-backed model client.

The client lazily imports litellm so the core framework, API, and tests run
without the optional ``[real]`` extra installed. Only an actual live run needs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"

    def __str__(self) -> str:  # nicer logs / event payloads
        return self.value


@dataclass(frozen=True)
class ModelSpec:
    """How a single role is configured."""

    model: str
    goal: str
    temperature: float = 0.2


@dataclass
class Usage:
    """Token + cost accounting, summable across calls."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            round(self.cost_usd + other.cost_usd, 6),
        )

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


class BudgetExceeded(RuntimeError):
    """Raised when cumulative run spend crosses the configured cap."""


class ModelClient:
    """Thin wrapper over ``litellm.completion`` with cost accounting.

    Keeps a running :class:`Usage` total and refuses further calls once the
    per-run budget is crossed, so a misbehaving loop can't run up a bill.
    """

    def __init__(self, budget_usd: float = 2.0) -> None:
        self.budget_usd = budget_usd
        self.total = Usage()

    def _check_budget(self) -> None:
        if self.total.cost_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"run spend ${self.total.cost_usd:.4f} reached cap ${self.budget_usd:.2f}"
            )

    def complete(self, spec: ModelSpec, messages: list[dict]) -> tuple[str, Usage]:
        """Run one completion. Returns (text, usage-for-this-call).

        Raises BudgetExceeded *before* the call if the cap is already hit.
        """
        self._check_budget()
        try:
            import litellm  # lazy: only needed for live runs
        except ImportError as exc:  # pragma: no cover - exercised only without [real]
            raise RuntimeError(
                "Live model calls need the optional backend. "
                'Install it with: pip install -e ".[real]"  (or use engine: mock)'
            ) from exc

        resp = litellm.completion(
            model=spec.model,
            messages=messages,
            temperature=spec.temperature,
        )
        text = resp["choices"][0]["message"]["content"] or ""
        usage = self._extract_usage(resp)
        self.total = self.total + usage
        return text, usage

    @staticmethod
    def _extract_usage(resp) -> Usage:
        u = getattr(resp, "usage", None) or {}
        pt = int(getattr(u, "prompt_tokens", 0) or (u.get("prompt_tokens", 0) if isinstance(u, dict) else 0))
        ct = int(getattr(u, "completion_tokens", 0) or (u.get("completion_tokens", 0) if isinstance(u, dict) else 0))
        cost = 0.0
        try:
            import litellm

            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:  # cost is best-effort; never fail a run over it
            cost = 0.0
        return Usage(pt, ct, cost)
