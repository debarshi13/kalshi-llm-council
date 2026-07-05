"""Paper-trading ledger: simulated fills, Kalshi fees, P&L, and calibration scoring.

A 'book' is one strategy's isolated bankroll + trade history. Scoring tracks
not just P&L but **calibration** (Brier score vs the market-price baseline) —
because over weeks, P&L is noisy and calibration is the real signal of edge.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .market import Market


def kalshi_fee(price: float, contracts: int, rate: float = 0.07) -> float:
    """Kalshi trading fee: ceil(rate · contracts · p · (1−p)), in dollars.

    Per Kalshi's published general fee formula. `price` is the contract's
    execution price in dollars (0–1); p·(1−p) is symmetric, so YES/NO give the
    same fee. Approximate — verify against Kalshi's current schedule before live.
    """
    raw_cents = rate * contracts * price * (1.0 - price) * 100
    # round away float noise (e.g. 175.0000000003) before ceiling to the next cent
    return math.ceil(round(raw_cents, 6)) / 100.0


MAKER_RATE_FRACTION = 0.25   # Kalshi maker fee = 25% of the taker rate


def maker_fee(price: float, contracts: int, rate: float = 0.07) -> float:
    """Maker-side Kalshi fee in dollars: 25% of the taker formula, ceil to next cent."""
    raw_cents = MAKER_RATE_FRACTION * rate * contracts * price * (1.0 - price) * 100
    return math.ceil(round(raw_cents, 6)) / 100.0


def required_edge(price: float, contracts: int = 10, spread_buffer: float = 0.01,
                  min_profit: float = 0.01) -> float:
    """Per-contract edge (dollars) a maker entry at `price` must clear to be worth placing.

    Fee is amortized over a nominal `contracts` size so the ceil-to-cent floor
    doesn't flatten the tails-vs-mid fee difference the strategy depends on.
    Settlement is fee-free on Kalshi, so only the entry fee is charged here.
    """
    return maker_fee(price, contracts) / max(contracts, 1) + spread_buffer + min_profit


@dataclass
class Trade:
    market_id: str
    side: str                  # "yes" | "no"
    contracts: int
    entry_price: float         # per-contract cost for the chosen side (dollars)
    yes_price_at_entry: float  # market YES price at entry (for the baseline)
    predicted_prob: float      # analyst's P(YES) — for calibration
    fee: float
    book: str = ""
    status: str = "open"       # "open" | "resolved"
    outcome: int | None = None
    net_pnl: float | None = None

    @property
    def cost(self) -> float:
        return round(self.contracts * self.entry_price + self.fee, 4)


class InsufficientCash(RuntimeError):
    pass


class PaperLedger:
    def __init__(self, bankroll: float, fee_rate: float = 0.07, book: str = "") -> None:
        self.starting_bankroll = bankroll
        self.cash = bankroll
        self.fee_rate = fee_rate
        self.book = book
        self.trades: list[Trade] = []

    def open_trade(self, market: Market, side: str, contracts: int, predicted_prob: float) -> Trade:
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        entry_price = market.yes_price if side == "yes" else market.no_price
        fee = kalshi_fee(entry_price, contracts, self.fee_rate)
        cost = contracts * entry_price + fee
        if cost > self.cash + 1e-9:
            raise InsufficientCash(f"cost ${cost:.2f} exceeds cash ${self.cash:.2f}")
        self.cash -= cost
        t = Trade(
            market_id=market.id, side=side, contracts=contracts, entry_price=entry_price,
            yes_price_at_entry=market.yes_price, predicted_prob=predicted_prob, fee=fee, book=self.book,
        )
        self.trades.append(t)
        return t

    def resolve_trade(self, trade: Trade, outcome: int) -> float:
        """Settle a trade against the YES outcome (1) or NO outcome (0)."""
        won = (trade.side == "yes" and outcome == 1) or (trade.side == "no" and outcome == 0)
        payout = trade.contracts * (1.0 if won else 0.0)
        self.cash += payout
        trade.status = "resolved"
        trade.outcome = outcome
        trade.net_pnl = round(payout - trade.contracts * trade.entry_price - trade.fee, 4)
        return trade.net_pnl

    @property
    def open_exposure(self) -> float:
        return round(sum(t.contracts * t.entry_price for t in self.trades if t.status == "open"), 4)

    def score(self) -> dict:
        resolved = [t for t in self.trades if t.status == "resolved"]
        realized = round(sum(t.net_pnl or 0.0 for t in resolved), 4)
        fees = round(sum(t.fee for t in self.trades), 4)
        wins = sum(1 for t in resolved if (t.net_pnl or 0) > 0)
        # Brier: analyst's P(YES) vs actual YES outcome, and the market baseline.
        brier = market_brier = None
        if resolved:
            brier = round(sum((t.predicted_prob - t.outcome) ** 2 for t in resolved) / len(resolved), 4)
            market_brier = round(
                sum((t.yes_price_at_entry - t.outcome) ** 2 for t in resolved) / len(resolved), 4
            )
        return {
            "book": self.book,
            "starting_bankroll": self.starting_bankroll,
            "cash": round(self.cash, 4),
            "realized_pnl": realized,
            "fee_drag": fees,
            "n_trades": len(self.trades),
            "n_resolved": len(resolved),
            "hit_rate": round(wins / len(resolved), 4) if resolved else None,
            "brier": brier,                 # lower is better
            "market_brier": market_brier,   # analyst beats market if brier < market_brier
            "beats_market": (brier is not None and brier < market_brier),
        }
