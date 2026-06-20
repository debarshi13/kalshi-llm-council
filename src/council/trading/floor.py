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
from datetime import date

from ..models import ModelClient, ModelSpec
from .analysts import MockResearch
from .deliberation import DeliberativeCouncil, decide
from .execution import KalshiTrader, RiskGuard
from .ledger import kalshi_fee
from .market import MockMarketData
from .research import WebSearchResearch, openrouter_online_search

# (book id, model name, key, mock-skill, OpenRouter slug, strategy prompt)
BOOKS = [
    ("A", "Claude", "claude", 0.56, "openrouter/anthropic/claude-opus-4.8", "news"),
    ("B", "Kimi K2", "kimi", 0.61, "openrouter/moonshotai/kimi-k2.6", "cross_source"),
    ("C", "GPT-5.4", "gpt", 0.50, "openrouter/openai/gpt-5.4", "reasoning"),
]


class FloorState:
    EDGE = 0.06
    MAX_PENDING = 2
    MAX_LIVE_CALLS = 300      # session backstop on top of the OpenRouter $ cap
    MIN_VOLUME = int(os.environ.get("MIN_MARKET_VOLUME", 50))  # below this a market can't reliably fill

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
        self._live_markets: list = []
        self.council = None
        self.research = None
        self.last_debate = None
        self.trades_today = 0
        self._day = date.today()
        self.DELIBERATE_EVERY = int(os.environ.get("DELIBERATE_EVERY", 5))
        self.MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 10))
        self._council_seen: set[str] = set()
        self.books["council"] = {"book": "★", "name": "Council", "key": "council",
                                 "skill": 0.5, "slug": "council", "strat": "consensus",
                                 "pnl": 0.0, "open": [], "wins": 0, "trades": 0}

    # ── control ─────────────────────────────────────────────────────────────
    def enable_live(self, budget: float = 10.0) -> None:
        client = ModelClient(budget_usd=budget)
        specs = [ModelSpec(bk["slug"], "Estimate a calibrated P(YES).")
                 for k, bk in self.books.items() if k != "council"]
        self.council = DeliberativeCouncil(specs, client)
        if os.environ.get("COUNCIL_RESEARCH", "online").lower() == "online":
            self.research = WebSearchResearch(openrouter_online_search(client))
        else:
            self.research = MockResearch()
        self._live_markets = self._load_live_markets()
        self.live = True
        self._log(f"LIVE armed — council active over {len(self._live_markets)} markets. Tokens will be spent.")

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
                mk = [m for m in KalshiMarketData(kid, pk).list_markets()
                      if 0.05 <= m.yes_price <= 0.95 and m.volume >= self.MIN_VOLUME]
                # Target thin, under-followed (but tradeable) markets — cheap_score ranks them.
                picks = sorted(mk, key=self.cheap_score, reverse=True)[:20] or mk[:20]
                if picks:
                    self._log(f"loaded {len(picks)} live Kalshi markets (thin-market focus)")
                    return picks
            except Exception as exc:  # noqa: BLE001
                self._log(f"Kalshi load failed ({type(exc).__name__}); falling back to mock markets")
        return self.markets

    def cheap_score(self, m) -> float:
        """Rank toward thin, under-followed markets — where the price is closest to a
        raw crowd guess and a researched council is likeliest to have an edge — but
        still tradeable. Below MIN_VOLUME a market is untradeable (-inf). Small bonus
        for tail prices (cheaper Kalshi fees, documented favorite-longshot bias)."""
        if m.volume < self.MIN_VOLUME:
            return float("-inf")
        thin = 1.0 / (1.0 + m.volume / 500.0)        # lower volume ranks higher
        tail_bonus = abs(0.5 - m.yes_price) * 0.5     # extreme prices = cheaper fees
        return thin + tail_bonus

    def _council_eval(self) -> None:
        if self.calls >= self.MAX_LIVE_CALLS:
            self.live = False
            self._log(f"live call cap ({self.MAX_LIVE_CALLS}) reached — LIVE auto-disabled")
            return
        today = date.today()
        if today != self._day:
            self._day = today
            self.trades_today = 0
            self._council_seen.clear()
        if self.trades_today >= self.MAX_TRADES_PER_DAY:
            return
        pool = [m for m in self._live_markets if m.id not in self._council_seen] or self._live_markets
        if not pool:
            return
        m = max(pool, key=self.cheap_score)
        self._council_seen.add(m.id)
        notes = self.research.context_for(m) if self.research else "No external signal available."
        d = self.council.debate(m, notes)
        dec = decide(d, m, self.guard)
        self.last_debate = (d, dec)
        self.calls += 1 + 2 * len(self.council.specs)  # 1 research + 2 rounds x N models
        self._log(f"Council debate {m.id}: P(YES) {d.converged_p:.2f} vs {m.yes_price:.2f} "
                  f"(spread {d.spread:.3f}) — {dec.reason}")
        if not dec.place:
            return
        entry = dec.limit_price_cents / 100.0
        if self.execute and not self.frozen:
            self._auto_execute("council", m, dec.side, entry, dec.contracts)
            self.trades_today += 1
        else:
            # LIVE but unarmed: book a PAPER position so the council can be evaluated
            # on real prices without risking money (settles on the mock timer below).
            bk = self.books["council"]
            bk["open"].append({"tk": m.id, "contracts": dec.contracts, "entry": entry,
                               "fee": kalshi_fee(entry, dec.contracts), "ttl": random.randint(2, 5)})
            self.trades_today += 1
            self._log(f"PAPER FILL — Council {dec.side.upper()} {dec.contracts} {m.id} @ {dec.limit_price_cents}¢")

    # ── tick ────────────────────────────────────────────────────────────────
    def tick(self) -> None:
        self._tick_n += 1
        if not self.execute:
            self._resolve_mock()       # only mock positions auto-settle; real ones settle on Kalshi
        if not self.auto or self.frozen:
            return
        if self.live:
            if self._tick_n % self.DELIBERATE_EVERY == 0:
                self._council_eval()
        else:
            self._mock_gen()

    def _mock_gen(self) -> None:
        for key, bk in self.books.items():
            if key == "council":
                continue
            if sum(1 for t in self.tickets if t["key"] == key) >= self.MAX_PENDING or random.random() < 0.45:
                continue
            m = random.choice(self.markets)
            prob = min(max(m.yes_price + random.gauss(0, 0.14), 0.02), 0.98)
            self._maybe_ticket(key, m, prob, thesis=None)

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

    def _debate_snapshot(self):
        if not self.last_debate:
            return None
        d, dec = self.last_debate
        return {
            "market_id": d.market_id,
            "converged_p": d.converged_p, "spread": d.spread, "notes": d.notes,
            "round1": [{"model": e.model, "p": e.p_yes, "thesis": e.thesis} for e in d.round1],
            "round2": [{"model": e.model, "p": e.p_yes, "thesis": e.thesis} for e in d.round2],
            "decision": {"place": dec.place, "side": dec.side, "contracts": dec.contracts,
                         "cents": dec.limit_price_cents, "reason": dec.reason},
        }

    def snapshot(self) -> dict:
        return {
            "output": round(self.output, 2),
            "frozen": self.frozen,
            "auto": self.auto,
            "live": self.live,
            "execute": self.execute,
            "calls": self.calls,
            "debate": self._debate_snapshot(),
            "books": [
                {"book": bk["book"], "name": bk["name"], "key": k, "pnl": round(bk["pnl"], 2),
                 "open": len(bk["open"]), "trades": bk["trades"],
                 "hit": round(bk["wins"] / bk["trades"] * 100) if bk["trades"] else None}
                for k, bk in self.books.items()
            ],
            "tickets": self.tickets,
            "activity": self.activity[:14],
        }
