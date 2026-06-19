"""Risk caps: the guardrails between a proposed trade and a (paper or real) fill.

Every proposed trade passes through `RiskCaps.check` before execution. The same
gate protects paper and live modes — so when COUNCIL_MODE flips to live, the
limits are already proven, not bolted on.
"""
from __future__ import annotations

from dataclasses import dataclass

from .ledger import PaperLedger


@dataclass
class RiskDecision:
    allowed: bool
    reason: str


@dataclass
class RiskCaps:
    bankroll: float
    max_position_frac: float = 0.05   # one trade's cost ≤ 5% of bankroll
    daily_loss_frac: float = 0.10     # halt the book after a 10% daily drawdown
    kill_switch: bool = False         # hard stop, denies everything

    def check(self, ledger: PaperLedger, proposed_cost: float, today_realized_pnl: float) -> RiskDecision:
        if self.kill_switch:
            return RiskDecision(False, "kill switch engaged")
        if proposed_cost > self.max_position_frac * self.bankroll + 1e-9:
            return RiskDecision(
                False,
                f"position ${proposed_cost:.2f} exceeds {self.max_position_frac:.0%} cap "
                f"(${self.max_position_frac * self.bankroll:.2f})",
            )
        if today_realized_pnl <= -self.daily_loss_frac * self.bankroll:
            return RiskDecision(
                False,
                f"daily loss halt: {today_realized_pnl:.2f} ≤ -{self.daily_loss_frac:.0%} of bankroll",
            )
        if proposed_cost > ledger.cash + 1e-9:
            return RiskDecision(False, f"insufficient cash (${ledger.cash:.2f})")
        return RiskDecision(True, "ok")
