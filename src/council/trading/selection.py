"""Market selection: score markets by how plausibly the price is WEAK.

Inverts the old cheap_score liquidity bias. We hunt tails (cheap fees +
favorite-longshot bias), under-followed volume (price ~ raw crowd, not yet
corrected by sharps), maker-viable spreads, and horizons short enough that
calibration data accrues — but never sub-hour lotteries.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .market import Market


@dataclass(frozen=True)
class SelectionParams:
    min_volume: int = 50          # below this, fills are unreliable (existing MIN_VOLUME)
    followed_volume: int = 20_000  # volume at which a market counts as fully "followed"
    min_spread: float = 0.02      # narrower than this leaves no room to post inside
    max_spread: float = 0.15      # wider than this, resting fills are unrealistic
    horizon_days: float = 7.0     # resolve soon enough that calibration accrues
    min_hours: float = 1.0        # exclude sub-hour lotteries (BTC hourly strikes)


def edge_score(m: Market, params: SelectionParams | None = None,
               now: float | None = None) -> float:
    """Composite 0..3 score; -inf = untradeable. Higher = weaker price, better target."""
    p = params or SelectionParams()
    now = now or time.time()
    if m.volume < p.min_volume:
        return -math.inf
    hrs = (m.close_ts - now) / 3600.0 if m.close_ts else None
    if hrs is not None and (hrs < p.min_hours or hrs > p.horizon_days * 24.0):
        return -math.inf
    tail = abs(m.yes_price - 0.5) * 2.0                                   # 0 mid .. 1 extreme
    under = 1.0 - min(m.volume / p.followed_volume, 1.0)                  # 1 thin .. 0 heavy
    spread = (m.yes_ask - m.yes_bid) if (m.yes_ask and m.yes_bid) else 0.0
    if p.min_spread <= spread <= p.max_spread:
        viability = 1.0
    elif spread > 0.0:
        viability = 0.3      # book exists but too tight/too wide to post inside
    else:
        viability = 0.0      # no book at all
    return tail + under + viability


def maker_price_cents(m: Market, side: str) -> int | None:
    """Resting limit inside the spread, in the side's own cents. None = no two-sided book."""
    if not (m.yes_bid and m.yes_ask):
        return None
    bid, ask = round(m.yes_bid * 100), round(m.yes_ask * 100)
    if ask - bid < 2:                       # no room to post inside
        return None
    if side == "yes":
        yes_px = bid + 1
    else:
        yes_px = ask - 1                    # improving the YES ask == bidding for NO
    yes_px = min(99, max(1, yes_px))
    return yes_px if side == "yes" else 100 - yes_px
