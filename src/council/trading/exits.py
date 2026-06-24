"""Pure exit-decision core — decides close/hold for an open position given a live quote.

No I/O, no LLM calls. Mirrors the scout/decide split: a deterministic function the floor
calls each tick. Money is in dollars (0.0-1.0), matching the rest of the trading package.
"""
from __future__ import annotations

from dataclasses import dataclass

from .market import Market


@dataclass(frozen=True)
class OpenPosition:
    trade_id: int
    market_id: str
    side: str            # "yes" | "no"
    entry_price: float   # the side's own fill price, dollars
    fair_value: float    # council converged probability, 0..1 (YES terms)
    contracts: int
    entry_fee: float


@dataclass(frozen=True)
class ExitParams:
    take_profit: float = 0.05      # close when up this many dollars vs entry
    exit_edge: float = 0.01        # close when remaining edge to fair value <= this
    stop_loss: float | None = None # if set, close when down this many dollars vs entry


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str = ""
    exit_price: float = 0.0        # the side's own price we'd sell into, dollars


def exit_signal(pos: OpenPosition, m: Market, params: ExitParams) -> ExitDecision:
    """Decide whether to close `pos` given live quote `m`. Triggers are OR'd.

    YES position: sell into the YES bid; fair value is the council prob.
    NO  position: sell into the NO bid (= 1 - yes_ask); fair value is (1 - prob).
    """
    if pos.side == "yes":
        bid = m.yes_bid
        fair = pos.fair_value
    else:  # "no" — short YES / long NO
        bid = round(1.0 - m.yes_ask, 4) if m.yes_ask else 0.0
        fair = round(1.0 - pos.fair_value, 4)

    if bid <= 0.0:                                   # no quote to price against
        return ExitDecision(False, "", 0.0)

    gain = round(bid - pos.entry_price, 4)
    remaining_edge = round(fair - bid, 4)

    if gain >= params.take_profit:
        return ExitDecision(True, f"take-profit +${gain:.2f}", bid)
    if remaining_edge <= params.exit_edge:
        return ExitDecision(True, f"edge decayed (${remaining_edge:.2f} <= ${params.exit_edge:.2f})", bid)
    if params.stop_loss is not None and (pos.entry_price - bid) >= params.stop_loss:
        return ExitDecision(True, f"stop-loss -${pos.entry_price - bid:.2f}", bid)
    return ExitDecision(False, "", bid)
