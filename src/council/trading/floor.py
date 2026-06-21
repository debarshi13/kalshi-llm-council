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
from .deliberation import DeliberativeCouncil, MockCouncil, decide
from .journal import Journal
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
    MAX_LIVE_CALLS = 300      # session backstop on top of the OpenRouter $ cap
    MIN_VOLUME = int(os.environ.get("MIN_MARKET_VOLUME", 50))  # below this a market can't reliably fill
    SPEAK_ORDER = ("claude", "gpt", "kimi")   # roundtable order; most capable (Kimi) speaks last

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
        self.guard = RiskGuard(
            max_position_usd=float(os.environ.get("MAX_POSITION_USD", 5)),
            max_total_exposure_usd=float(os.environ.get("MAX_TOTAL_USD", 50)),
            max_daily_loss_usd=float(os.environ.get("MAX_DAILY_LOSS_USD", 20)),
        )
        self.calls = 0
        self._ids = itertools.count(1)
        self._tick_n = 0
        self._live_markets: list = []
        self.last_debate = None
        self.trades_today = 0
        self._day = date.today()
        self.DELIBERATE_EVERY = int(os.environ.get("DELIBERATE_EVERY", 5))
        self.MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 20))
        # Daytrade gate (env-tunable): how small an edge to act on, how much model
        # disagreement to tolerate. Looser = more trades, weaker edges.
        self.EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", 0.03))
        self.SPREAD_CAP = float(os.environ.get("SPREAD_CAP", 0.08))
        self._council_seen: set[str] = set()
        self.journal = Journal(os.environ.get("JOURNAL_PATH", ":memory:"))
        self._last_fill = None
        self.MARK_EVERY = int(os.environ.get("MARK_EVERY", 20))
        self.RESOLVE_EVERY = int(os.environ.get("RESOLVE_EVERY", 40))
        self._market_data = None
        self.books["council"] = {"book": "★", "name": "Council", "key": "council",
                                 "skill": 0.5, "slug": "council", "strat": "consensus",
                                 "pnl": 0.0, "open": [], "wins": 0, "trades": 0}
        # Free PAPER mode deliberates with a mock council (no API calls) so it
        # produces ONE consensus decision per market — same shape as LIVE.
        self.council = MockCouncil([self.books[k]["name"] for k in self.SPEAK_ORDER if k in self.books])
        self.research = MockResearch()

    # ── control ─────────────────────────────────────────────────────────────
    def enable_live(self, budget: float = 10.0) -> None:
        client = ModelClient(budget_usd=budget)
        # Speaking order matters: Kimi K2 (most capable) speaks LAST so it hears the others.
        specs = [ModelSpec(self.books[k]["slug"], "roundtable analyst")
                 for k in self.SPEAK_ORDER if k in self.books]
        self.council = DeliberativeCouncil(specs, client)
        if os.environ.get("COUNCIL_RESEARCH", "online").lower() == "online":
            self.research = WebSearchResearch(openrouter_online_search(client))
        else:
            self.research = MockResearch()
        self._live_markets = self._load_live_markets()
        self.live = True
        if self._live_markets:
            self._log(f"LIVE — council active over {len(self._live_markets)} real Kalshi markets. Tokens will be spent.")
        else:
            self._log("⚠ LIVE ON but NO real markets loaded — council will idle (no debates, no orders). See warning above.")

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

    def _auto_execute(self, key, m, side, entry, contracts) -> bool:
        """Place a real order that actually crosses the spread, and book ONLY what fills.
        Returns True iff at least one contract filled. Cross: pay the ask to buy YES, hit
        the bid to buy NO (= sell YES). Falls back to the decided entry if no live quote."""
        bk = self.books[key]
        if side == "yes":
            cross = round((m.yes_ask or entry) * 100)
        else:  # buy NO == sell YES at the bid
            cross = 100 - round((m.yes_bid or (1 - entry)) * 100)
        cross = min(99, max(1, int(cross)))
        cost = contracts * entry
        exposure = sum(p["contracts"] * p["entry"] for b in self.books.values() for p in b["open"])
        ok, reason = self.guard.check(cost, exposure, self.output)
        if not ok:
            self._log(f"RISK BLOCKED — {bk['name']} {side.upper()} {m.id}: {reason}")
            return False
        try:
            resp = self.trader.place_order(m.id, side, contracts, cross)
        except Exception as exc:  # noqa: BLE001 — never let a broker error crash the loop
            self._log(f"ORDER FAILED — {bk['name']} {m.id}: {type(exc).__name__}")
            return False
        filled = int(float((resp or {}).get("fill_count", 0) or 0))
        if filled <= 0:
            self._log(f"NO FILL — {bk['name']} {side.upper()} {m.id} (limit didn't cross)")
            return False
        # average_fill_price is in YES terms; book the position in the side's own price
        yes_px = float((resp or {}).get("average_fill_price", cross / 100.0) or cross / 100.0)
        fill_px = yes_px if side == "yes" else round(1 - yes_px, 4)
        bk["open"].append({"tk": m.id, "contracts": filled, "entry": fill_px,
                           "fee": kalshi_fee(fill_px, filled), "ttl": 9_999})
        self._last_fill = {"price": fill_px, "count": filled}   # for the journal
        self._log(f"LIVE ORDER FILLED — {bk['name']} {side.upper()} {filled} {m.id} @ {round(fill_px*100)}¢")
        return True

    def _load_live_markets(self) -> list:
        """Real, tradeable Kalshi markets — or [] (NEVER mock). In live mode the floor
        must refuse to trade rather than silently place orders against fake tickers."""
        kid, pk = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not (kid and pk):
            self._log("⚠ NO LIVE MARKETS — Kalshi credentials missing; live trading disabled.")
            return []
        try:
            from .market import KalshiMarketData
            raw = KalshiMarketData(kid, pk).list_markets()
            mk = [m for m in raw if 0.05 <= m.yes_price <= 0.95 and m.volume >= self.MIN_VOLUME]
            # Target thin, under-followed (but tradeable) markets — cheap_score ranks them.
            picks = sorted(mk, key=self.cheap_score, reverse=True)[:20]
            if picks:
                self._log(f"loaded {len(picks)} live Kalshi markets (of {len(raw)} fetched, liquid-market focus)")
                return picks
            self._log(f"⚠ NO TRADEABLE LIVE MARKETS — {len(raw)} fetched, none cleared "
                      f"vol≥{self.MIN_VOLUME} & price 0.05–0.95; live trading disabled.")
            return []
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ Kalshi market load FAILED ({type(exc).__name__}: {exc}); live trading disabled.")
            return []

    def cheap_score(self, m) -> float:
        """Daytrade ranking: liquid AND closing soon. Prefers markets with rich research
        (volume) that resolve imminently (volume per hour-to-close), so the council acts on
        fresh, fast-resolving bets. Below MIN_VOLUME a market is untradeable (-inf)."""
        if m.volume < self.MIN_VOLUME:
            return float("-inf")
        hrs = max((m.close_ts - time.time()) / 3600.0, 0.25) if m.close_ts else 9999.0
        return m.volume / hrs

    def _council_eval(self) -> None:
        # Token backstop only bounds LIVE (real-spend) mode; free mock runs unbounded.
        if self.live and self.calls >= self.MAX_LIVE_CALLS:
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
        markets = self._live_markets if self.live else self.markets
        pool = [m for m in markets if m.id not in self._council_seen]
        if not pool:                       # debated them all — rotate fresh
            self._council_seen.clear()
            pool = list(markets)
        if not pool:
            return
        m = max(pool, key=self.cheap_score)
        self._council_seen.add(m.id)
        notes = self.research.context_for(m) if self.research else "No external signal available."
        lessons = self.journal.recall(m.id) if self.journal else ""
        d = self.council.debate(m, notes, lessons)
        dec = decide(d, m, self.guard, edge_threshold=self.EDGE_THRESHOLD, spread_cap=self.SPREAD_CAP)
        self.last_debate = (d, dec)
        if self.live:
            self.calls += 1 + 2 * len(self.council.specs)  # 1 research + 2 rounds x N models
        self._log(f"Council debate {m.id}: P(YES) {d.converged_p:.2f} vs {m.yes_price:.2f} "
                  f"(spread {d.spread:.3f}) — {dec.reason}")
        if not dec.place:
            self._journal_log(m, d, dec, side=dec.side or "n/a", fill_price=0.0,
                              contracts=0, edge=0.0, status="skipped")
            return
        entry = dec.limit_price_cents / 100.0
        if self.execute and not self.frozen:
            self._last_fill = None
            if self._auto_execute("council", m, dec.side, entry, dec.contracts):
                self.trades_today += 1
                fill = self._last_fill or {"price": entry, "count": dec.contracts}
                self._journal_log(m, d, dec, side=dec.side, fill_price=fill["price"],
                                  contracts=fill["count"],
                                  edge=abs(d.converged_p - m.yes_price), status="placed")
        elif self.live:
            # LIVE but unarmed: book a PAPER position so the council can be evaluated
            # on real prices without risking money (settles on the mock timer below).
            self.books["council"]["open"].append(
                {"tk": m.id, "contracts": dec.contracts, "entry": entry,
                 "fee": kalshi_fee(entry, dec.contracts), "ttl": random.randint(2, 5)})
            self.trades_today += 1
            self._journal_log(m, d, dec, side=dec.side, fill_price=entry,
                              contracts=dec.contracts,
                              edge=abs(d.converged_p - m.yes_price), status="placed")
            self._log(f"PAPER FILL — Council {dec.side.upper()} {dec.contracts} {m.id} @ {dec.limit_price_cents}¢")
        else:
            self._council_ticket(m, d, dec)   # free PAPER mode: one ticket to ship/reject

    def _journal_log(self, m, d, dec, *, side, fill_price, contracts, edge, status) -> None:
        if not self.journal:
            return
        rationale = " | ".join(f"{t.model} {t.p_yes:.2f}" for t in d.round2)
        self.journal.log(market_id=m.id, title=m.title, side=side, converged_p=d.converged_p,
                         spread=d.spread, market_price=m.yes_price,
                         executable_price=dec.limit_price_cents / 100.0, edge=edge,
                         contracts=contracts, fill_price=fill_price,
                         fee=kalshi_fee(fill_price, contracts) if contracts else 0.0,
                         fill_count=contracts, decision_reason=dec.reason,
                         rationale=rationale, status=status)

    def _council_ticket(self, m, d, dec) -> None:
        """One consensus ticket for the user to ship/reject. Deduped per market so a
        market already awaiting your decision isn't re-proposed every interval."""
        if any(t["tk"] == m.id for t in self.tickets):
            return
        self.tickets.append({
            "id": next(self._ids), "key": "council", "book": "★", "who": "Council",
            "tk": m.id, "ti": m.title, "side": dec.side, "prob": round(d.converged_p, 2),
            "price": round(m.yes_price, 2), "contracts": dec.contracts,
            "entry": dec.limit_price_cents / 100.0, "thesis": dec.reason,
        })

    # ── tick ────────────────────────────────────────────────────────────────
    def tick(self) -> None:
        self._tick_n += 1
        if not self.execute:
            self._resolve_mock()       # mock + paper positions auto-settle; real ones settle on Kalshi
        if not self.auto or self.frozen:
            return
        if self._tick_n % self.DELIBERATE_EVERY == 0:
            self._council_eval()       # one deliberation per interval — mock OR live
        if self._tick_n % self.MARK_EVERY == 0:
            self.mark_open()           # mark-to-market the journal's open real positions
        if self._tick_n % self.RESOLVE_EVERY == 0:
            self.resolve_settled()     # backfill true outcomes once markets settle

    def _md(self):
        if self._market_data is None:
            from .market import KalshiMarketData
            kid, pk = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
            self._market_data = KalshiMarketData(kid, pk) if kid and pk else None
        return self._market_data

    def _open_market_ids(self):
        return [r["market_id"] for r in self.journal._c.execute(
            "SELECT DISTINCT market_id FROM trades WHERE status='placed'").fetchall()]

    def mark_open(self) -> None:
        md = self._md()
        if not md:
            return
        for mid in self._open_market_ids():
            try:
                m = md.get_market(mid)
                if m:
                    self.journal.mark(mid, m.yes_price)
            except Exception:  # noqa: BLE001 — marking must never crash the loop
                pass

    def resolve_settled(self) -> None:
        md = self._md()
        if not md:
            return
        for mid in self._open_market_ids():
            try:
                m = md.get_market(mid)
                if m and m.status == "resolved" and m.outcome is not None:
                    self.journal.resolve(mid, "yes" if m.outcome == 1 else "no")
            except Exception:  # noqa: BLE001
                pass

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
