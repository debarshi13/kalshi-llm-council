"""Deliberative council: a two-round debate over one market + a pure decision rule.

The decision rule is deterministic — no model places a trade. The council produces a
converged probability and a disagreement (spread); `decide` gates the order on a fee-aware
edge AND consensus, and sizes within the position cap.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from ..models import ModelClient, ModelSpec
from .analysts import parse_estimate
from .execution import RiskGuard
from .market import Market


@dataclass
class ModelEstimate:
    model: str
    p_yes: float
    thesis: str


@dataclass
class Deliberation:
    market_id: str
    round1: list[ModelEstimate]
    round2: list[ModelEstimate]
    converged_p: float
    spread: float
    notes: str = ""


@dataclass
class Decision:
    place: bool
    side: str | None
    contracts: int
    limit_price_cents: int
    reason: str


def decide(d: Deliberation, market: Market, caps: RiskGuard,
           edge_threshold: float = 0.06, spread_cap: float = 0.05,
           full_conviction_edge: float = 0.20) -> Decision:
    # Executable prices: pay the ask to buy YES, hit the bid to sell (buy NO). Fall back to
    # the mid when there's no live quote (keeps legacy behavior + existing tests stable).
    ask = market.yes_ask or market.yes_price
    bid = market.yes_bid or market.yes_price
    yes_edge = d.converged_p - ask         # buy YES profit per contract
    no_edge = bid - d.converged_p          # buy NO == sell YES at the bid
    if yes_edge >= no_edge:
        side, edge, entry = "yes", yes_edge, ask
    else:
        side, edge, entry = "no", no_edge, round(1 - bid, 2)
    price_cents = min(99, max(1, int(round(entry * 100))))
    if d.spread > spread_cap:
        return Decision(False, None, 0, price_cents,
                        f"no consensus (spread {d.spread:.3f} > {spread_cap:.3f})")
    if edge < edge_threshold:
        return Decision(False, None, 0, price_cents,
                        f"exec edge {edge*100:.1f}c < {edge_threshold*100:.0f}c threshold (vs live quote) -> SKIP")
    # Conviction-scaled sizing on the EXECUTABLE edge (within RiskGuard caps).
    span = max(full_conviction_edge - edge_threshold, 1e-9)
    conviction = min(1.0, (edge - edge_threshold) / span)
    agreement = 1.0 - min(d.spread / spread_cap, 1.0)
    size_frac = 0.3 + 0.7 * conviction * agreement
    contracts = max(1, int((caps.max_position_usd * size_frac) / max(entry, 0.05)))
    return Decision(True, side, contracts, price_cents,
                    f"{edge*100:.1f}c {side.upper()} exec-edge, spread {d.spread:.3f}, "
                    f"size {size_frac*100:.0f}% -> PLACE")


import re

_PYES_RE = re.compile(r"p\s*\(?\s*yes\s*\)?\s*[:=]\s*([01]?\.?\d+)\s*(%?)", re.I)

_ROUNDTABLE_SYS = (
    "You are {name}, one of three sharp prediction-market analysts at a roundtable with "
    "Claude, GPT-5.4, and Kimi K2. You are pricing ONE market together. The CURRENT MARKET "
    "PRICE is a STRONG PRIOR — it already reflects the crowd and informed traders. Only "
    "deviate materially from it if you can name a SPECIFIC CATALYST the market is missing; "
    "absent a concrete reason, converge toward the price. Read the resolution rules carefully "
    "(misreading the threshold or direction is the most common, costly error). Read the "
    "research and what your colleagues have said, engage directly (agree, push back, refine), "
    "keep it to 2-3 sentences, and END with a line exactly:\nP(YES): <number between 0 and 1>"
)


def _market_block(market: Market, notes: str) -> str:
    quote = (f"Live quote: YES bid {market.yes_bid:.2f} / ask {market.yes_ask:.2f}\n"
             if (market.yes_ask or market.yes_bid) else "")
    rules = f"Resolution rules: {market.rules}\n" if market.rules else ""
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"Current YES price: {market.yes_price:.2f}\n"
            f"{quote}{rules}Research notes:\n{notes}\n")


def _name_for(slug: str) -> str:
    s = slug.lower()
    if "claude" in s:
        return "Claude"
    if "kimi" in s:
        return "Kimi K2"
    if "gpt" in s or "openai" in s:
        return "GPT-5.4"
    return slug.split("/")[-1]


def _parse_prob(text: str, market: Market) -> float:
    """Extract the final 'P(YES): x' from a roundtable message; accepts 0-1 or a percent.
    Falls back to the JSON/prob parser, then to the market price (claim no edge)."""
    matches = _PYES_RE.findall(text)
    if matches:
        val, pct = matches[-1]            # the LAST stated number is the final answer
        p = float(val)
        if pct or p > 1:
            p /= 100.0
        return min(max(p, 0.0), 1.0)
    return parse_estimate(text, market).prob_yes


class DeliberativeCouncil:
    def __init__(self, specs: list[ModelSpec], client: ModelClient) -> None:
        self.specs = specs            # speaking order; the most capable model should be last
        self.client = client

    def debate(self, market: Market, notes: str) -> Deliberation:
        """A real roundtable: each model speaks once, in order, seeing the full conversation
        so far. The system+research prefix is identical across turns (cache-friendly); the
        growing transcript rides in the user message. The last speaker hears everyone."""
        prefix = _market_block(market, notes)
        transcript: list[ModelEstimate] = []
        convo = ""
        for spec in self.specs:
            name = _name_for(spec.model)
            system = _ROUNDTABLE_SYS.format(name=name) + "\n\n" + prefix
            user = (convo or "You speak first — open the discussion.") + \
                   f"\n\nYou are {name}. Give your take and end with 'P(YES): <0-1>'."
            text, _ = self.client.complete(
                spec, [{"role": "system", "content": system}, {"role": "user", "content": user}])
            msg = text.strip()
            transcript.append(ModelEstimate(name, _parse_prob(msg, market), msg))
            convo += f"\n{name}: {msg}\n"
        ps = [t.p_yes for t in transcript]
        converged = sum(ps) / len(ps)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, [], transcript, round(converged, 4), round(spread, 4), notes)


class MockCouncil:
    """Offline stand-in for DeliberativeCouncil — produces a single converged
    deliberation per market with no API calls, so the floor's free PAPER mode
    deliberates exactly like LIVE (one decision per market) instead of emitting
    independent, contradictory per-model tickets."""

    def __init__(self, model_names: list[str]) -> None:
        self.specs = list(model_names)   # only len() is read by the floor

    def debate(self, market: Market, notes: str) -> Deliberation:
        import random
        base = market.yes_price
        transcript: list[ModelEstimate] = []
        for name in self.specs:           # speaking order (Kimi last, as the floor builds it)
            p = round(min(max(base + random.gauss(0, 0.08), 0.02), 0.98), 3)
            transcript.append(ModelEstimate(name, p, f"{name}: I read this around {p:.0%}. P(YES): {p:.2f}"))
        ps = [t.p_yes for t in transcript]
        converged = sum(ps) / len(ps)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, [], transcript, round(converged, 4), round(spread, 4), notes)
