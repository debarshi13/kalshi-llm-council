"""The desk: runs strategy books A/B/C side by side over one watchlist.

Each book is an isolated bankroll + analyst; the desk ticks them all and
reports a leaderboard so weeks of paper data show which (if any) book has a
real, fee-aware, well-calibrated edge before any real money is risked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ModelClient, ModelSpec
from .analysts import LiteLLMAnalyst, MockResearch, ResearchProvider
from .book import Analyst, Book, MockAnalyst, Trade
from .ledger import PaperLedger
from .market import MarketData
from .risk import RiskCaps

# (book id, display name, strategy-prompt key)
STRATEGIES = [
    ("A", "News-driven", "news"),
    ("B", "Cross-source", "cross_source"),
    ("C", "Calibrated reasoning", "reasoning"),
]


@dataclass
class Desk:
    books: list[Book]
    market_data: MarketData
    watchlist: list[str]

    def run_tick(self, today_pnl: dict[str, float] | None = None) -> dict[str, list[Trade]]:
        today_pnl = today_pnl or {}
        return {
            b.name: b.run_tick(self.market_data, self.watchlist, today_pnl.get(b.name, 0.0))
            for b in self.books
        }

    def leaderboard(self) -> list[dict]:
        """Per-book scores, best realized P&L first."""
        return sorted((b.ledger.score() for b in self.books), key=lambda s: s["realized_pnl"], reverse=True)


def _book(book_id: str, name: str, analyst: Analyst, bankroll: float, **risk_kw) -> Book:
    return Book(
        name=book_id,
        analyst=analyst,
        ledger=PaperLedger(bankroll, book=book_id),
        risk=RiskCaps(bankroll=bankroll, **risk_kw),
    )


def build_litellm_desk(
    market_data: MarketData,
    *,
    client: ModelClient,
    spec: ModelSpec,
    research: ResearchProvider | None = None,
    bankroll: float = 1000.0,
    watchlist: list[str] | None = None,
) -> Desk:
    """Live desk: three books sharing one model client, differing only by strategy prompt."""
    research = research or MockResearch()
    books = [
        _book(bid, name, LiteLLMAnalyst(spec, strat, client, research), bankroll)
        for bid, name, strat in STRATEGIES
    ]
    return Desk(books, market_data, watchlist or [m.id for m in market_data.list_markets()])


def build_mock_desk(
    market_data: MarketData,
    *,
    beliefs: dict[str, dict[str, float]] | None = None,
    bankroll: float = 1000.0,
    watchlist: list[str] | None = None,
) -> Desk:
    """Offline desk: each book gets a MockAnalyst (optionally with per-book beliefs)."""
    beliefs = beliefs or {}
    books = [
        _book(bid, name, MockAnalyst(beliefs.get(bid)), bankroll) for bid, name, _ in STRATEGIES
    ]
    return Desk(books, market_data, watchlist or [m.id for m in market_data.list_markets()])
