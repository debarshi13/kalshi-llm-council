"""Scout funnel: cheap pre-screen before the expensive council debate.

Three tiers:
  Tier 0 — structural_score + shortlist (free, pure Python)
  Tier 1 — one cheap LLM call to triage the shortlist (pick)
  Tier 2 — full council debate (existing roundtable, unchanged)
"""
from __future__ import annotations

import json
import re
import time

from ..models import ModelClient, ModelSpec
from .journal import Journal
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


class Scout:
    """Three-tier funnel: structural filter -> cheap LLM triage -> escalate."""

    def __init__(self, client: ModelClient, model: str, shortlist_n: int, max_escalate: int) -> None:
        self.client = client
        self.model = model
        self.shortlist_n = shortlist_n
        self.max_escalate = max_escalate

    def shortlist(self, markets: list[Market]) -> list[Market]:
        """Tier 0: pure-Python structural ranking. No API calls.
        Returns up to self.shortlist_n markets, best-first."""
        if not markets:
            return []
        ranked = sorted(markets, key=structural_score, reverse=True)
        return ranked[:self.shortlist_n]

    def pick(self, markets: list[Market], journal: Journal) -> list[Market]:
        """Tier 1: one cheap LLM call over the Tier-0 shortlist.
        Returns 0..max_escalate markets flagged as genuinely mispriced, or [].
        Injects journal calibration + per-market recall as context."""
        if not markets:
            return []

        market_by_id = {m.id: m for m in markets}
        prompt = self._build_prompt(markets, journal)
        spec = ModelSpec(self.model, goal="scout")

        try:
            text, _ = self.client.complete(
                spec, [{"role": "system", "content": self._system_prompt()},
                       {"role": "user", "content": prompt}])
        except Exception:
            return []  # fail closed

        return self._parse_response(text, market_by_id)

    def _system_prompt(self) -> str:
        return (
            "You are a prediction-market scout. Your job is to identify markets that "
            "look genuinely mispriced from a shortlist of candidates. You must name a "
            "SPECIFIC CATALYST — what the market is missing — to flag a market. Without "
            "a concrete reason, return NONE.\n\n"
            "Respond with ONLY a JSON object: {\"escalate\": [\"TICKER-1\", ...]} "
            "with the ticker IDs of markets worth escalating to a full debate, or "
            "{\"escalate\": []} if none look promising."
        )

    def _build_prompt(self, markets: list[Market], journal: Journal) -> str:
        cal = journal.calibration()
        lines = ["# Your track record"]
        o = cal["overall"]
        if o["n"] > 0:
            lines.append(f"Overall: {o['win']}/{o['n']} correct ({round(100 * o['win'] / o['n'])}%)")
            for bucket, data in cal["by_edge"].items():
                if data["n"] > 0:
                    lines.append(f"  {bucket} edges: {data['win']}/{data['n']} correct")
        else:
            lines.append("No resolved history yet.")

        lines.append("\n# Candidate markets")
        for m in markets:
            hrs = max((m.close_ts - time.time()) / 3600.0, 0.25) if m.close_ts else None
            hrs_str = f"{hrs:.1f}h to close" if hrs and hrs < 9999 else "unknown close"
            spread_str = f"bid {m.yes_bid:.2f} / ask {m.yes_ask:.2f}" if (m.yes_bid or m.yes_ask) else "no quote"
            rules_summary = (m.rules[:200] + "...") if len(m.rules) > 200 else m.rules
            lessons = journal.recall(m.id)
            lines.append(
                f"\n## {m.id}: {m.title}\n"
                f"YES price: {m.yes_price:.2f} | {spread_str} | vol: {m.volume} | {hrs_str}\n"
                f"Rules: {rules_summary or 'none'}\n"
                f"{lessons}"
            )

        return "\n".join(lines)

    def _parse_response(self, text: str, market_by_id: dict[str, Market]) -> list[Market]:
        """Defensive parse: extract market IDs, validate against input, cap at max_escalate.
        On any failure, return [] (fail closed)."""
        try:
            # Try JSON parse first
            data = json.loads(text)
            ids = data.get("escalate", [])
        except (json.JSONDecodeError, AttributeError):
            # Fallback: try to extract JSON from mixed text
            match = re.search(r'\{[^}]*"escalate"\s*:\s*\[([^\]]*)\][^}]*\}', text)
            if not match:
                return []
            try:
                data = json.loads(match.group(0))
                ids = data.get("escalate", [])
            except (json.JSONDecodeError, AttributeError):
                return []

        if not isinstance(ids, list):
            return []

        result = []
        for ticker in ids:
            if not isinstance(ticker, str):
                continue
            if ticker in market_by_id:
                result.append(market_by_id[ticker])
            if len(result) >= self.max_escalate:
                break
        return result
