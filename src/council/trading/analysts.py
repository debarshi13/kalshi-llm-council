"""Analysts — the only part that differs between strategy books A/B/C.

Each analyst turns a market + research context into a calibrated P(YES) + thesis.
`LiteLLMAnalyst` is the live implementation (prompts a model via ModelClient);
the strategy is just a different system prompt and a different research feed.
Parsing is defensive: if the model doesn't return a clean probability, the
analyst defers to the market price (i.e. claims no edge) rather than inventing one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from ..models import ModelClient, ModelSpec
from .book import Estimate
from .market import Market

# Strategy = a system-prompt persona. The research feed (below) supplies the data.
STRATEGY_PROMPTS = {
    "news": (
        "You are a news-driven prediction-market analyst. Using the latest news and events "
        "in the research notes, judge whether the market's current price is stale and about to move."
    ),
    "cross_source": (
        "You are a cross-source analyst. Compare the market price against external probabilities "
        "(polls, economic data, futures-implied odds) in the research notes, and trade the divergence."
    ),
    "reasoning": (
        "You are a calibrated first-principles analyst working thin, under-followed markets. "
        "Reason from base rates and structure; do not anchor on the crowd."
    ),
}

_JSON_RE = re.compile(r"\{.*\}", re.S)
_PROB_RE = re.compile(r'"?prob(?:ability)?_?yes"?\s*[:=]\s*([0-9]*\.?[0-9]+)', re.I)


class ResearchProvider(Protocol):
    def context_for(self, market: Market) -> str: ...


class MockResearch:
    """Offline research feed: canned notes per market id (default: none)."""

    def __init__(self, notes: dict[str, str] | None = None) -> None:
        self.notes = notes or {}

    def context_for(self, market: Market) -> str:
        return self.notes.get(market.id, "No external signal available.")


def parse_estimate(text: str, market: Market) -> Estimate:
    """Extract P(YES) + thesis from a model reply; defer to market price if unparseable."""
    prob: float | None = None
    thesis = text.strip()[:200] or "no thesis"
    m = _JSON_RE.search(text)
    if m:
        try:
            d = json.loads(m.group(0))
            prob = float(d["prob_yes"])
            thesis = str(d.get("thesis", thesis))
        except (ValueError, KeyError, TypeError):
            prob = None
    if prob is None:
        mm = _PROB_RE.search(text)
        if mm:
            prob = float(mm.group(1))
    if prob is None:
        prob = market.yes_price  # no clean signal -> claim no edge
    return Estimate(prob_yes=min(max(prob, 0.0), 1.0), thesis=thesis)


@dataclass
class LiteLLMAnalyst:
    spec: ModelSpec
    strategy: str            # key into STRATEGY_PROMPTS
    client: ModelClient
    research: ResearchProvider

    def estimate(self, market: Market) -> Estimate:
        system = (
            STRATEGY_PROMPTS[self.strategy]
            + ' Respond ONLY with JSON: {"prob_yes": <0..1>, "thesis": "<one sentence>"}.'
        )
        user = (
            f"Market: {market.title} (ticker {market.id})\n"
            f"Current YES price: {market.yes_price:.2f}\n"
            f"Research notes:\n{self.research.context_for(market)}\n\n"
            "Give your calibrated P(YES) and a one-sentence thesis."
        )
        text, _ = self.client.complete(
            self.spec, [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return parse_estimate(text, market)
