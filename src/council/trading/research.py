"""Research feed: turn a market into a few dated, sourced snippets for the debate.

`WebSearchResearch` wraps an injected `search_fn` so tests pass a fake and no network is
touched. `openrouter_online_search` is the live backend (OpenRouter ':online' web plugin) —
one call per market, shared across all debaters. Failures degrade to the no-signal string;
they never raise into the trading loop.
"""
from __future__ import annotations

from typing import Callable

from ..models import ModelClient, ModelSpec
from .market import Market

_NONE = "No external signal available."


class WebSearchResearch:
    def __init__(self, search_fn: Callable[[str], str]) -> None:
        self._search = search_fn

    def context_for(self, market: Market) -> str:
        try:
            notes = self._search(market.title or market.id)
        except Exception:  # noqa: BLE001 — never let research crash the loop
            return _NONE
        return notes.strip() or _NONE


def openrouter_online_search(client: ModelClient,
                             model_slug: str = "openrouter/openai/gpt-4o-mini:online") -> Callable[[str], str]:
    def _search(query: str) -> str:
        spec = ModelSpec(model_slug, "Gather decision-relevant market research.")
        text, _ = client.complete(spec, [
            {"role": "system", "content":
                "You are a research assistant with web access. Reply with 3-5 short, dated, "
                "sourced bullet points relevant to the prediction market. No preamble."},
            {"role": "user", "content":
                f"Find recent, decision-relevant facts for this prediction market: {query}"},
        ])
        return text
    return _search
