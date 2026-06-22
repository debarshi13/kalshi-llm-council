"""Scout funnel: cheap pre-screen before the expensive council debate.

Three tiers:
  Tier 0 — structural_score + shortlist (free, pure Python)
  Tier 1 — one cheap LLM call to triage the shortlist (pick)
  Tier 2 — full council debate (existing roundtable, unchanged)
"""
from __future__ import annotations

import time

from .market import Market


def structural_score(m: Market) -> float:
    """Tier-0 composite mispricing signal. Pure, deterministic, no API calls.

    Four equally-weighted components (each normalized to roughly 0-1):
      1. Stale-price-vs-volume: volume / hours-to-close (same idea as cheap_score)
      2. Favorite-longshot tails: distance from 0.5 (extremes are interesting)
      3. Wide bid/ask spread: wider = potentially mispriced
      4. Closing-soon recency: inverse hours to close
    """
    # 1. Volume intensity (volume per hour to close; higher = busier + sooner)
    hrs = max((m.close_ts - time.time()) / 3600.0, 0.25) if m.close_ts else 9999.0
    vol_intensity = m.volume / hrs

    # Normalize to ~0-1 range (10k vol/hr is very high)
    vol_score = min(vol_intensity / 10000.0, 1.0)

    # 2. Tail detection: how far from 0.5 (extremes have behavioral bias)
    tail_score = abs(m.yes_price - 0.5) * 2.0  # 0 at mid, 1.0 at extremes

    # 3. Bid/ask spread width (wider = potentially mispriced or illiquid)
    spread = (m.yes_ask - m.yes_bid) if (m.yes_ask and m.yes_bid) else 0.0
    spread_score = min(max(spread, 0.0) / 0.20, 1.0)  # cap at 20c spread

    # 4. Closing-soon urgency (inverse hours; sooner = more actionable)
    urgency_score = min(1.0 / hrs, 1.0) if hrs < 9999.0 else 0.0

    # Equal weights (tunable later from journal data)
    return vol_score + tail_score + spread_score + urgency_score
