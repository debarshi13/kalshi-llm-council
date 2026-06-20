"""Live, monitorable paper floor — the state the web UI polls.

Two independent switches the UI controls:
  • AUTO  — is the floor generating new work at all? (off = paused, approve-only)
  • LIVE  — mock random edges (free) vs. real LiteLLM analysts (spends tokens)

Defaults are AUTO on + LIVE off, so out of the box the floor shows free mock
activity you can watch and approve at zero cost. Flipping LIVE on arms the real
models (Claude/Kimi/GLM over live Kalshi prices) and is rate-limited + capped.
"""
from __future__ import annotations

import itertools
import os
import random
import time

from ..models import ModelClient, ModelSpec
from .analysts import LiteLLMAnalyst, MockResearch
from .execution import KalshiTrader, RiskGuard
from .ledger import kalshi_fee
from .market import MockMarketData

# (book id, model name, key, mock-skill, OpenRouter slug, strategy prompt)
BOOKS = [
    ("A", "Claude", "claude", 0.56, "openrouter/anthropic/claude-opus-4.8", "news"),
    ("B", "Kimi K2", "kimi", 0.61, "openrouter/moonshotai/kimi-k2.6", "cross_source"),
    ("C", "GLM-5.2", "glm", 0.43, "openrouter/z-ai/glm-5.2", "reasoning"),
]


class FloorState:
    EDGE = 0.06
    MAX_PENDING = 2
    LIVE_EVERY = 5            # evaluate one live (book,market) every Nth tick (cost pacing)
    MAX_LIVE_CALLS = 300      # session backstop on top of the OpenRouter $ cap

    def __init__(self) -> None:
        self.markets = MockMarketData().list_markets()
        self.books = {
            key: {"book": b, "name": name, "key": key, "skill": skill, "slug": slug, "strat": strat,
                  "pnl": 0.0, "open": [], "wins": 0, "trades": 0}
            for b, name, key, skill, slug, strat in BOOKS
        }
        self.output = 0.0
        self.tickets: list[dict] = []
        self.activity: list[dict] = []
        self.frozen = False
        self.auto = True
        self.live = False
        self.execute = False        # placing REAL orders (armed)
        self.trader: KalshiTrader | None = None
        self.guard: RiskGuard | None = None
        self.calls = 0
        self._ids = itertools.count(1)
        self._tick_n = 0
        self._rr = 0
        self._analysts: dict | None = None
        self._live_markets: list = []

    # ── control ─────────────────────────────────────────────────────────────
    def enable_live(self, budget: float = 10.0) -> None:
        client = ModelClient(budget_usd=budget)
        research = MockResearch()  # web-search feed is a later upgrade
        self._analysts = {
            k: LiteLLMAnalyst(ModelSpec(bk["slug"], "Estimate a calibrated P(YES)."),
                              bk["strat"], client, research)
            for k, bk in self.books.items()
        }
        self._live_markets = self._load_live_markets()
        self.live = True
        self._log(f"LIVE armed — real models active over {len(self._live_markets)} markets. Tokens will be spent.")

    def arm_execution(self, host: str | None = None) -> None:
        """Turn on REAL order placement. Gated behind COUNCIL_MODE=live + Kalshi creds."""
        if os.environ.get("COUNCIL_MODE", "paper").lower() != "live":
            raise RuntimeError("set COUNCIL_MODE=live in .env before arming real execution")
        kid, pk = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not (kid and pk):
            raise RuntimeError("Kalshi credentials missing from .env")
        if not self.live:
            self.enable_live()
        self.trader = KalshiTrader(kid, pk, host=host or os.environ.get("KALSHI_HOST"))
        self.guard = RiskGuard(
            max_position_usd=float(os.environ.get("MAX_POSITION_USD", 5)),
            max_total_exposure_usd=float(os.environ.get("MAX_TOTAL_USD", 50)),
            max_daily_loss_usd=float(os.environ.get("MAX_DAILY_LOSS_USD", 20)),
        )
        self.execute = True
        self._log(f"LIVE EXECUTION ARMED — real orders now place automatically. Caps: "
                  f"position ${self.guard.max_position_usd:.0f} · total ${self.guard.max_total_exposure_usd:.0f} · "
                  f"daily-loss ${self.guard.max_daily_loss_usd:.0f}.")

    def _auto_execute(self, key, m, side, entry, contracts) -> None:
        bk = self.books[key]
        cost = contracts * entry
        exposure = sum(p["contracts"] * p["entry"] for b in self.books.values() for p in b["open"])
        ok, reason = self.guard.check(cost, exposure, self.output)
        if not ok:
            self._log(f"RISK BLOCKED — {bk['name']} {side.upper()} {m.id}: {reason}")
            return
        try:
            self.trader.place_order(m.id, side, contracts, round(entry * 100))
        except Exception as exc:  # noqa: BLE001 — never let a broker error crash the loop
            self._log(f"ORDER FAILED — {bk['name']} {m.id}: {type(exc).__name__}")
            return
        bk["open"].append({"tk": m.id, "contracts": contracts, "entry": entry,
                           "fee": kalshi_fee(entry, contracts), "ttl": 9_999})
        self._log(f"LIVE ORDER PLACED — {bk['name']} {side.upper()} {contracts} {m.id} @ {round(entry*100)}¢")

    def _load_live_markets(self) -> list:
        kid, pk = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if kid and pk:
            try:
                from .market import KalshiMarketData
                mk = [m for m in KalshiMarketData(kid, pk).list_markets() if 0.05 <= m.yes_price <= 0.95]
                liquid = [m for m in mk if m.volume > 500][:10] or mk[:10]
                if liquid:
                    self._log(f"loaded {len(liquid)} live Kalshi markets")
                    return liquid
            except Exception as exc:  # noqa: BLE001
                self._log(f"Kalshi load failed ({type(exc).__name__}); falling back to mock markets")
        return self.markets

    # ── tick ────────────────────────────────────────────────────────────────
    def tick(self) -> None:
        self._tick_n += 1
        if not self.live:
            self._resolve_mock()       # only mock positions auto-settle; real ones settle on Kalshi
        if not self.auto or self.frozen:
            return
        if self.live:
            if self._tick_n % self.LIVE_EVERY == 0:
                self._live_eval()
        else:
            self._mock_gen()

    def _mock_gen(self) -> None:
        for key, bk in self.books.items():
            if sum(1 for t in self.tickets if t["key"] == key) >= self.MAX_PENDING or random.random() < 0.45:
                continue
            m = random.choice(self.markets)
            prob = min(max(m.yes_price + random.gauss(0, 0.14), 0.02), 0.98)
            self._maybe_ticket(key, m, prob, thesis=None)

    def _live_eval(self) -> None:
        if self.calls >= self.MAX_LIVE_CALLS:
            self.live = False
            self._log(f"live call cap ({self.MAX_LIVE_CALLS}) reached — LIVE auto-disabled")
            return
        keys = list(self._analysts)
        key = keys[self._rr % len(keys)]
        self._rr += 1
        if sum(1 for t in self.tickets if t["key"] == key) >= self.MAX_PENDING:
            return
        m = random.choice(self._live_markets)
        bk = self.books[key]
        try:
            est = self._analysts[key].estimate(m)
            self.calls += 1
        except Exception as exc:  # noqa: BLE001
            self._log(f"{bk['name']} model error: {type(exc).__name__}")
            return
        self._log(f"{bk['name']}: {m.id} → P(YES) {est.prob_yes:.2f} vs {m.yes_price:.2f} · {est.thesis[:70]}")
        self._maybe_ticket(key, m, est.prob_yes, thesis=est.thesis)

    def _maybe_ticket(self, key: str, m, prob: float, thesis: str | None) -> None:
        edge = prob - m.yes_price
        if abs(edge) < self.EDGE:
            return
        bk = self.books[key]
        side = "yes" if edge > 0 else "no"
        entry = m.yes_price if side == "yes" else round(1 - m.yes_price, 2)
        contracts = max(1, int(50 / max(entry, 0.05)))
        if self.execute and not self.frozen:
            self._auto_execute(key, m, side, entry, contracts)   # autonomous: no human gate
            return
        self.tickets.append({
            "id": next(self._ids), "key": key, "book": bk["book"], "who": f"{bk['name']} · {bk['book']}",
            "tk": m.id, "ti": m.title, "side": side, "prob": round(prob, 2),
            "price": round(m.yes_price, 2), "contracts": contracts, "entry": entry,
            "thesis": thesis or f"{abs(edge)*100:.0f}¢ edge vs the market.",
        })
        self._log(f"{bk['name']} wants {side.upper()} {m.id} ({abs(edge)*100:.0f}¢ edge)")

    def _resolve_mock(self) -> None:
        for bk in self.books.values():
            for pos in bk["open"][:]:
                pos["ttl"] -= 1
                if pos["ttl"] <= 0:
                    win = random.random() < bk["skill"]
                    pnl = round(pos["contracts"] * ((1 if win else 0) - pos["entry"]) - pos["fee"], 2)
                    self.output = round(self.output + pnl, 2)
                    bk["pnl"] = round(bk["pnl"] + pnl, 2)
                    bk["trades"] += 1
                    bk["wins"] += 1 if pnl > 0 else 0
                    bk["open"].remove(pos)
                    self._log(f"{bk['name']} {'WON' if win else 'lost'} {pos['tk']} ({'+' if pnl>=0 else ''}{pnl:.2f})")

    # ── approve / snapshot ───────────────────────────────────────────────────
    def approve(self, ticket_id: int, granted: bool) -> bool:
        t = next((x for x in self.tickets if x["id"] == ticket_id), None)
        if not t:
            return False
        self.tickets.remove(t)
        bk = self.books[t["key"]]
        if granted:
            ttl = random.randint(2, 5) if not self.live else 9_999  # live trades settle on real Kalshi, not a timer
            bk["open"].append({"tk": t["tk"], "contracts": t["contracts"], "entry": t["entry"],
                               "fee": kalshi_fee(t["entry"], t["contracts"]), "ttl": ttl})
            self._log(f"SHIPPED — {t['who']} {t['side'].upper()} {t['contracts']} {t['tk']}")
        else:
            self._log(f"rejected — {t['who']} {t['tk']}")
        return True

    def _log(self, s: str) -> None:
        self.activity.insert(0, {"t": time.strftime("%H:%M:%S"), "s": s})
        del self.activity[40:]

    def snapshot(self) -> dict:
        return {
            "output": round(self.output, 2),
            "frozen": self.frozen,
            "auto": self.auto,
            "live": self.live,
            "execute": self.execute,
            "calls": self.calls,
            "books": [
                {"book": bk["book"], "name": bk["name"], "key": k, "pnl": round(bk["pnl"], 2),
                 "open": len(bk["open"]), "trades": bk["trades"],
                 "hit": round(bk["wins"] / bk["trades"] * 100) if bk["trades"] else None}
                for k, bk in self.books.items()
            ],
            "tickets": self.tickets,
            "activity": self.activity[:14],
        }
