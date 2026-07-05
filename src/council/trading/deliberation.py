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
from .calibrate import extremize
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

_BLIND_SYS = (
    "You are {name}, a sharp prediction-market analyst. Estimate the probability that "
    "the market below resolves YES, using ONLY the resolution rules, the research notes, "
    "and your own knowledge. You are deliberately NOT shown the market price — form an "
    "independent view. Read the resolution rules carefully (misreading the threshold or "
    "direction is the most common, costly error). 2-3 sentences of reasoning, then END "
    "with a line exactly:\nP(YES): <number between 0 and 1>"
)

_CONVERGE_SYS = (
    "You are {name}, at a roundtable with two other analysts. You all just estimated this "
    "market blind; now the market price and everyone's blind estimates are revealed. The "
    "price is ONE input: it reflects the crowd, but thin or under-followed books can be "
    "stale or biased. Revise your estimate only for a SPECIFIC stated reason (a rule "
    "misread, information a colleague raised, or a crowd bias you can name) — do not "
    "reflexively defer to the price, and do not move just to agree. 2-3 sentences, then "
    "END with a line exactly:\nP(YES): <number between 0 and 1>"
)


def _market_block(market: Market, notes: str, lessons: str = "") -> str:
    quote = (f"Live quote: YES bid {market.yes_bid:.2f} / ask {market.yes_ask:.2f}\n"
             if (market.yes_ask or market.yes_bid) else "")
    rules = f"Resolution rules: {market.rules}\n" if market.rules else ""
    les = f"{lessons}\n" if lessons else ""
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"Current YES price: {market.yes_price:.2f}\n"
            f"{quote}{rules}{les}Research notes:\n{notes}\n")


def _blind_block(market: Market, notes: str, lessons: str = "") -> str:
    rules = f"Resolution rules: {market.rules}\n" if market.rules else ""
    les = f"{lessons}\n" if lessons else ""
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"{rules}{les}Research notes:\n{notes}\n")


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


def _parse_prob_blind(text: str) -> float | None:
    """Blind-round parser: NEVER falls back to the market price."""
    matches = _PYES_RE.findall(text)
    if not matches:
        return None
    val, pct = matches[-1]
    p = float(val)
    if pct or p > 1:
        p /= 100.0
    return min(max(p, 0.0), 1.0)


class DeliberativeCouncil:
    def __init__(self, specs: list[ModelSpec], client: ModelClient, alpha: float = 1.3) -> None:
        self.specs = specs            # speaking order; the most capable model should be last
        self.client = client
        self.alpha = alpha            # extremization strength (EXTREMIZE_ALPHA)

    def debate(self, market: Market, notes: str, lessons: str = "") -> Deliberation:
        """Round 1: every model estimates BLIND (no price, no peers) — independent signal.
        Round 2: price + all blind estimates revealed; models may revise with a reason.
        Converged = extremized MEDIAN of round 2 (median resists one outlier model).

        NOTE: round1 may be EMPTY if no model produced a parseable blind estimate
        (i.e., lacked a 'P(YES): x' line). Consumers computing median(round1) must
        guard: `blind_p = median(round1) if round1 else None`."""
        blind = _blind_block(market, notes, lessons)
        round1: list[ModelEstimate] = []
        for spec in self.specs:
            name = _name_for(spec.model)
            system = _BLIND_SYS.format(name=name) + "\n\n" + blind
            text, _ = self.client.complete(
                spec, [{"role": "system", "content": system},
                       {"role": "user", "content": f"You are {name}. Give your independent estimate."}])
            msg = text.strip()
            p = _parse_prob_blind(msg)
            if p is not None:                       # unparseable blind turn adds no signal
                round1.append(ModelEstimate(name, p, msg))

        reveal = _market_block(market, notes, lessons) + "\nBlind estimates:\n" + \
            "\n".join(f"- {e.model}: P(YES) {e.p_yes:.2f} — {e.thesis}" for e in round1)
        round2: list[ModelEstimate] = []
        for spec in self.specs:
            name = _name_for(spec.model)
            system = _CONVERGE_SYS.format(name=name) + "\n\n" + reveal
            text, _ = self.client.complete(
                spec, [{"role": "system", "content": system},
                       {"role": "user", "content": f"You are {name}. Give your final estimate."}])
            msg = text.strip()
            round2.append(ModelEstimate(name, _parse_prob(msg, market), msg))

        ps = [t.p_yes for t in round2]
        converged = extremize(statistics.median(ps), self.alpha)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, round1, round2, round(converged, 4), round(spread, 4), notes)


class MockCouncil:
    """Offline stand-in for DeliberativeCouncil — produces a single converged
    deliberation per market with no API calls, so the floor's free PAPER mode
    deliberates exactly like LIVE (one decision per market) instead of emitting
    independent, contradictory per-model tickets."""

    def __init__(self, model_names: list[str]) -> None:
        self.specs = list(model_names)   # only len() is read by the floor

    def debate(self, market: Market, notes: str, lessons: str = "") -> Deliberation:
        import random
        round1 = [ModelEstimate(n, round(min(max(0.5 + random.gauss(0, 0.15), 0.02), 0.98), 3),
                                f"{n}: blind take.") for n in self.specs]
        round2 = []
        for name in self.specs:
            p = round(min(max(market.yes_price + random.gauss(0, 0.08), 0.02), 0.98), 3)
            round2.append(ModelEstimate(name, p, f"{name}: I read this around {p:.0%}. P(YES): {p:.2f}"))
        ps = [t.p_yes for t in round2]
        converged = sum(ps) / len(ps)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, round1, round2, round(converged, 4), round(spread, 4), notes)
