"""A paper-trading book: one strategy's evaluation loop over a watchlist.

A book pairs an `Analyst` (the strategy's edge logic) with a ledger and risk
caps. `run_tick` evaluates each watched market: the analyst estimates P(YES),
the book compares it to the market price, and if the edge clears a threshold
(and risk allows) it opens a paper position. The analyst is the only part that
differs between strategy books A/B/C; everything else is shared.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .ledger import InsufficientCash, PaperLedger, Trade
from .market import Market, MarketData
from .risk import RiskCaps


@dataclass
class Estimate:
    prob_yes: float        # analyst's calibrated P(YES), 0–1
    thesis: str            # one-line rationale (for the dashboard / audit)


class Analyst(Protocol):
    def estimate(self, market: Market) -> Estimate: ...


class MockAnalyst:
    """Offline analyst: returns preset P(YES) per market id (default = market price).

    With the default (price), it sees no edge and never trades — a useful null
    baseline. Pass `beliefs` to simulate a strategy that disagrees with the market.
    """

    def __init__(self, beliefs: dict[str, float] | None = None) -> None:
        self.beliefs = beliefs or {}

    def estimate(self, market: Market) -> Estimate:
        p = self.beliefs.get(market.id, market.yes_price)
        return Estimate(prob_yes=p, thesis=f"mock estimate {p:.2f} vs price {market.yes_price:.2f}")


@dataclass
class Book:
    name: str
    analyst: Analyst
    ledger: PaperLedger
    risk: RiskCaps
    edge_threshold: float = 0.05   # require ≥5¢ edge over price to bother (clears fees)

    def run_tick(self, market_data: MarketData, watchlist: list[str], today_pnl: float = 0.0) -> list[Trade]:
        """Evaluate each watched market once; open paper trades where edge + risk allow."""
        opened: list[Trade] = []
        for mid in watchlist:
            market = market_data.get_market(mid)
            if market is None or market.status != "open":
                continue
            est = self.analyst.estimate(market)
            edge = est.prob_yes - market.yes_price  # +ve → YES underpriced; −ve → overpriced

            if edge >= self.edge_threshold:
                side, entry_price = "yes", market.yes_price
            elif edge <= -self.edge_threshold:
                side, entry_price = "no", market.no_price
            else:
                continue  # no actionable edge

            # Size to the position cap, then risk-check the actual cost.
            contracts = int((self.risk.max_position_frac * self.risk.bankroll) / entry_price)
            if contracts < 1:
                continue
            proposed_cost = contracts * entry_price + 0.0  # fee added at fill; cap check is conservative
            decision = self.risk.check(self.ledger, proposed_cost, today_pnl)
            if not decision.allowed:
                continue
            try:
                opened.append(self.ledger.open_trade(market, side, contracts, est.prob_yes))
            except InsufficientCash:
                continue
        return opened
