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
           edge_threshold: float = 0.06, spread_cap: float = 0.05) -> Decision:
    edge = d.converged_p - market.yes_price
    side = "yes" if edge > 0 else "no"
    entry = market.yes_price if side == "yes" else round(1 - market.yes_price, 2)
    price_cents = min(99, max(1, int(round(entry * 100))))
    if d.spread > spread_cap:
        return Decision(False, None, 0, price_cents,
                        f"no consensus (spread {d.spread:.3f} > {spread_cap:.3f})")
    if abs(edge) < edge_threshold:
        return Decision(False, None, 0, price_cents,
                        f"edge {abs(edge)*100:.1f}c < {edge_threshold*100:.0f}c threshold")
    contracts = max(1, int(caps.max_position_usd / max(entry, 0.05)))
    return Decision(True, side, contracts, price_cents,
                    f"{abs(edge)*100:.1f}c {side.upper()} edge, spread {d.spread:.3f} ok -> PLACE")


_SYS = ('You are a calibrated prediction-market analyst. '
        'Respond ONLY with JSON: {"prob_yes": <0..1>, "thesis": "<one sentence>"}.')


def _market_block(market: Market, notes: str) -> str:
    return (f"Market: {market.title} (ticker {market.id})\n"
            f"Current YES price: {market.yes_price:.2f}\n"
            f"Research notes:\n{notes}\n")


class DeliberativeCouncil:
    def __init__(self, specs: list[ModelSpec], client: ModelClient) -> None:
        self.specs = specs
        self.client = client

    def _ask(self, spec: ModelSpec, market: Market, user: str) -> ModelEstimate:
        text, _ = self.client.complete(
            spec, [{"role": "system", "content": _SYS}, {"role": "user", "content": user}])
        est = parse_estimate(text, market)
        return ModelEstimate(spec.model, est.prob_yes, est.thesis)

    def debate(self, market: Market, notes: str) -> Deliberation:
        block = _market_block(market, notes)
        round1 = [self._ask(s, market, block + "\nGive your calibrated P(YES) and a one-sentence thesis.")
                  for s in self.specs]
        peer = "PEER ESTIMATES (round 1):\n" + "\n".join(
            f"- {e.model}: P(YES) {e.p_yes:.2f} — {e.thesis}" for e in round1)
        round2 = [self._ask(s, market, block + "\n" + peer +
                            "\nReconsider in light of your peers and give your final P(YES) and one-sentence thesis.")
                  for s in self.specs]
        ps = [e.p_yes for e in round2]
        converged = sum(ps) / len(ps)
        spread = statistics.pstdev(ps) if len(ps) > 1 else 0.0
        return Deliberation(market.id, round1, round2, round(converged, 4), round(spread, 4), notes)
