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
from .scout import Scout, MockScout
from .journal import Journal
from .execution import KalshiTrader, RiskGuard
from .ledger import kalshi_fee, maker_fee
from .market import MockMarketData
from .research import WebSearchResearch, openrouter_online_search
from .selection import SelectionParams, edge_score, maker_price_cents

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
        self.SPREAD_BUFFER = float(os.environ.get("SPREAD_BUFFER", 0.01))
        self.MIN_PROFIT = float(os.environ.get("MIN_PROFIT", 0.01))
        self.SPREAD_CAP = float(os.environ.get("SPREAD_CAP", 0.08))
        # Markets debated recently are skipped until this cooldown elapses, then become
        # eligible again (prices move — a SKIP now may be a trade later). Sustains the
        # escalation rate instead of permanently retiring everything it has looked at.
        self.COUNCIL_COOLDOWN_SEC = int(os.environ.get("COUNCIL_COOLDOWN_MIN", 30)) * 60
        self._council_seen_ts: dict[str, float] = {}   # market_id -> last-debated epoch
        self.journal = Journal(os.environ.get("JOURNAL_PATH", ":memory:"))
        self.open_orders: list[dict] = []      # resting maker orders awaiting fill/TTL
        self.ORDER_TTL_TICKS = int(os.environ.get("ORDER_TTL_TICKS", 30))
        self.MARK_EVERY = int(os.environ.get("MARK_EVERY", 20))
        self.RESOLVE_EVERY = int(os.environ.get("RESOLVE_EVERY", 40))
        self._market_data = None
        self.vault_dir = os.environ.get("COUNCIL_VAULT")   # set to the Obsidian vault to mirror trades
        self.books["council"] = {"book": "★", "name": "Council", "key": "council",
                                 "skill": 0.5, "slug": "council", "strat": "consensus",
                                 "pnl": 0.0, "open": [], "wins": 0, "trades": 0}
        # Free PAPER mode deliberates with a mock council (no API calls) so it
        # produces ONE consensus decision per market — same shape as LIVE.
        self.council = MockCouncil([self.books[k]["name"] for k in self.SPEAK_ORDER if k in self.books])
        self.research = MockResearch()
        # Scout funnel: cheap pre-screen before expensive council debate.
        self._scout_model = os.environ.get("SCOUT_MODEL", "openrouter/moonshotai/kimi-k2.6")
        self._scout_shortlist = int(os.environ.get("SCOUT_SHORTLIST", 8))
        self._scout_max_escalate = int(os.environ.get("SCOUT_MAX_ESCALATE", 1))
        self._scout_max_per_series = int(os.environ.get("SCOUT_MAX_PER_SERIES", 2))
        if self._scout_model:
            self.scout = MockScout(shortlist_n=self._scout_shortlist,
                                   max_escalate=self._scout_max_escalate,
                                   max_per_series=self._scout_max_per_series)
        else:
            self.scout = None  # scout disabled — fallback to old cheap_score path

        # Exit layer — cheap, deterministic position management (no LLM calls).
        from .exits import ExitParams
        _sl = os.environ.get("EXIT_STOP_LOSS")
        self._exit_params = ExitParams(
            take_profit=float(os.environ.get("EXIT_TAKE_PROFIT", 0.05)),
            exit_edge=float(os.environ.get("EXIT_EDGE", 0.01)),
            stop_loss=float(_sl) if _sl else None,
        )

        # Selection: hunt weak prices (tails, under-followed, maker-viable spreads).
        self._sel_params = SelectionParams(
            min_volume=self.MIN_VOLUME,
            horizon_days=float(os.environ.get("EDGE_HORIZON_DAYS", 7)),
        )

    # ── control ─────────────────────────────────────────────────────────────
    def enable_live(self, budget: float = 10.0) -> None:
        client = ModelClient(budget_usd=budget)
        # Speaking order matters: Kimi K2 (most capable) speaks LAST so it hears the others.
        specs = [ModelSpec(self.books[k]["slug"], "roundtable analyst")
                 for k in self.SPEAK_ORDER if k in self.books]
        self.council = DeliberativeCouncil(
            specs, client, alpha=float(os.environ.get("EXTREMIZE_ALPHA", 1.3)))
        if self._scout_model:
            self.scout = Scout(client=client, model=self._scout_model,
                               shortlist_n=self._scout_shortlist,
                               max_escalate=self._scout_max_escalate,
                               max_per_series=self._scout_max_per_series)
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

    def _place_maker(self, m, d, dec, paper: bool) -> bool:
        """Post a resting order INSIDE the spread (maker: 75% fee discount, earn the
        spread). Booked only when a poll sees a real fill. Returns True iff posted."""
        entry = dec.limit_price_cents / 100.0
        cost = dec.contracts * entry
        exposure = sum(p["contracts"] * p["entry"] for b in self.books.values() for p in b["open"]) \
            + sum(o["contracts"] * o["px"] for o in self.open_orders)
        daily_pnl = self.journal.realized_today() if self.journal else self.output
        ok, reason = self.guard.check(cost, exposure, daily_pnl)
        if not ok:
            self._log(f"RISK BLOCKED — Council {dec.side.upper()} {m.id}: {reason}")
            return False
        if paper:
            order_id = f"paper-{next(self._ids)}"
        else:
            try:
                resp = self.trader.place_order(m.id, dec.side, dec.contracts,
                                               dec.limit_price_cents, tif="gtc")
            except Exception as exc:  # noqa: BLE001 — never let a broker error crash the loop
                self._log(f"ORDER FAILED — Council {m.id}: {type(exc).__name__}")
                return False
            order_id = str(((resp or {}).get("order") or {}).get("order_id") or "")
            if not order_id:
                self._log(f"ORDER REJECTED — Council {m.id}: no order_id in response")
                return False
        tid = self._journal_log(m, d, dec, side=dec.side, fill_price=0.0,
                                contracts=dec.contracts, edge=0.0, status="working")
        self.open_orders.append({"order_id": order_id, "journal_id": tid, "key": "council",
                                 "tk": m.id, "side": dec.side, "contracts": dec.contracts,
                                 "px": entry, "tick": self._tick_n, "paper": paper})
        self._log(f"{'PAPER ' if paper else ''}MAKER POSTED — Council {dec.side.upper()} "
                  f"{dec.contracts} {m.id} @ {dec.limit_price_cents}¢ (rests, TTL {self.ORDER_TTL_TICKS} ticks)")
        return True

    def _poll_orders(self) -> None:
        """Each tick: book any filled resting order; cancel any past its TTL."""
        if not self.open_orders:
            return
        md = self._md() if any(o["paper"] for o in self.open_orders) else None
        for o in self.open_orders[:]:
            filled, fill_px = 0, o["px"]
            if o["paper"]:
                m = None
                try:
                    m = md.get_market(o["tk"]) if md else None
                except Exception:  # noqa: BLE001
                    pass
                if m is not None:
                    # A resting bid fills when the far side crosses down to it.
                    if o["side"] == "yes" and m.yes_ask and m.yes_ask <= o["px"] + 1e-9:
                        filled = o["contracts"]
                    if o["side"] == "no" and m.yes_bid and (1 - m.yes_bid) <= o["px"] + 1e-9:
                        filled = o["contracts"]
            else:
                try:
                    resp = self.trader.get_order(o["order_id"])
                except Exception:  # noqa: BLE001 — poll again next tick
                    continue
                st = (resp or {}).get("order") or {}
                filled = int(float(st.get("fill_count") or 0))
                if filled:
                    fill_px_yes = float(st.get("average_fill_price") or o["px"])
                    fill_px = fill_px_yes if o["side"] == "yes" else round(1 - fill_px_yes, 4)
            if filled > 0:
                fee = maker_fee(fill_px, filled)
                self.books[o["key"]]["open"].append(
                    {"tk": o["tk"], "contracts": filled, "entry": fill_px, "fee": fee,
                     "ttl": 9_999 if not o["paper"] else random.randint(2, 5)})
                self.journal.mark_filled(o["journal_id"], fill_px, fee, filled)
                self._mirror(o["journal_id"])
                self.trades_today += 1
                self.open_orders.remove(o)
                self._log(f"{'PAPER ' if o['paper'] else ''}MAKER FILLED — Council "
                          f"{o['side'].upper()} {filled} {o['tk']} @ {round(fill_px*100)}¢")
            elif self._tick_n - o["tick"] >= self.ORDER_TTL_TICKS:
                if not o["paper"]:
                    try:
                        self.trader.cancel_order(o["order_id"])
                    except Exception:  # noqa: BLE001 — dropping tracking is still safe: journal row cancels
                        pass
                self.journal.cancel(o["journal_id"])
                self.open_orders.remove(o)
                self._log(f"MAKER EXPIRED — {o['side'].upper()} {o['tk']} unfilled after "
                          f"{self.ORDER_TTL_TICKS} ticks, cancelled")

    def _exit_eval(self) -> None:
        """Each tick: price every open real position off a fresh quote and close it if a
        trigger fires. Bypasses the risk guard — closing only reduces exposure."""
        if not (self.live and self.execute and self.journal and not self.frozen):
            return
        md = self._md()
        if not md:
            return
        from .exits import OpenPosition, exit_signal
        for row in self.journal.open_positions():
            try:
                m = md.get_market(row["market_id"])
            except Exception:  # noqa: BLE001 — pricing must never crash the loop
                continue
            if m is None:                       # can't price it this tick — skip
                continue
            pos = OpenPosition(trade_id=row["id"], market_id=row["market_id"], side=row["side"],
                               entry_price=row["fill_price"], fair_value=row["converged_p"],
                               contracts=row["contracts"], entry_fee=row["fee"] or 0.0)
            sig = exit_signal(pos, m, self._exit_params)
            if sig.should_exit:
                self._place_exit(pos, m, sig)

    def _place_exit(self, pos, m, sig) -> None:
        """Cross the spread to SELL the side we hold, book the realized P&L, free exposure."""
        if pos.side == "yes":
            cross = round((m.yes_bid or sig.exit_price) * 100)
        else:                                   # selling NO -> hit the NO bid (= 1 - yes_ask)
            cross = round((1 - m.yes_ask if m.yes_ask else (1 - sig.exit_price)) * 100)
        cross = min(99, max(1, int(cross)))
        try:
            resp = self.trader.place_order(pos.market_id, pos.side, pos.contracts, cross, action="sell")
        except Exception as exc:  # noqa: BLE001 — never let a broker error crash the loop
            self._log(f"EXIT ORDER FAILED — {pos.market_id}: {type(exc).__name__}")
            return
        filled = int(float((resp or {}).get("fill_count", 0) or 0))
        if filled <= 0:
            self._log(f"EXIT NO FILL — {pos.side.upper()} {pos.market_id} (limit didn't cross)")
            return
        yes_px = float((resp or {}).get("average_fill_price", cross / 100.0) or cross / 100.0)
        exit_px = yes_px if pos.side == "yes" else round(1 - yes_px, 4)
        realized = self.journal.record_exit(pos.trade_id, exit_px, kalshi_fee(exit_px, filled), filled)
        self.books["council"]["open"] = [p for p in self.books["council"]["open"]
                                         if p["tk"] != pos.market_id]
        self._mirror(pos.trade_id)
        self._log(f"EXIT FILLED — {pos.side.upper()} {filled} {pos.market_id} @ "
                  f"{round(exit_px * 100)}¢ ({sig.reason}) realized ${realized:.2f}")

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
            mk = [m for m in raw if 0.03 <= m.yes_price <= 0.97]
            picks = sorted((m for m in mk if edge_score(m, self._sel_params) > float("-inf")),
                           key=lambda m: edge_score(m, self._sel_params), reverse=True)[:20]
            if picks:
                self._log(f"loaded {len(picks)} live Kalshi markets (of {len(raw)} fetched, weak-price focus)")
                return picks
            self._log(f"⚠ NO TRADEABLE LIVE MARKETS — {len(raw)} fetched, none cleared edge_score gates "
                      f"(vol≥{self.MIN_VOLUME}, price 0.03–0.97, horizon)")
            return []
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ Kalshi market load FAILED ({type(exc).__name__}: {exc}); live trading disabled.")
            return []

    def cheap_score(self, m) -> float:
        """Fallback ranking when the scout is disabled — same weak-price scoring."""
        return edge_score(m, self._sel_params)

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
            self._council_seen_ts.clear()
        if self.trades_today >= self.MAX_TRADES_PER_DAY:
            return
        markets = self._live_markets if self.live else self.markets
        now = time.time()
        # Skip markets debated within the cooldown; they re-enter once it elapses.
        pool = [m for m in markets
                if now - self._council_seen_ts.get(m.id, 0.0) >= self.COUNCIL_COOLDOWN_SEC]
        if not pool:                       # everything debated recently — wait it out
            return

        # ── scout funnel (new) ─────────────────────────────────────────────
        if self.scout is not None:
            shortlist = self.scout.shortlist(pool)
            escalated = self.scout.pick(shortlist, self.journal) if shortlist else []
            if self.live and shortlist:
                self.calls += 1  # one cheap scout call
            if not escalated:
                self._log("Scout: no candidates escalated — skipping debate")
                return
            m = escalated[0]
        else:
            # Backward compat: SCOUT_MODEL="" disables the scout
            m = max(pool, key=self.cheap_score)

        self._council_seen_ts[m.id] = now
        notes = self.research.context_for(m) if self.research else "No external signal available."
        lessons = self.journal.recall(m.id) if self.journal else ""
        d = self.council.debate(m, notes, lessons)
        dec = decide(d, m, self.guard, spread_cap=self.SPREAD_CAP,
                     spread_buffer=self.SPREAD_BUFFER, min_profit=self.MIN_PROFIT)
        self.last_debate = (d, dec)
        if self.live:
            self.calls += 1 + 2 * len(self.council.specs)  # 1 research + 2 rounds per model
        self._log(f"Council debate {m.id}: P(YES) {d.converged_p:.2f} vs {m.yes_price:.2f} "
                  f"(spread {d.spread:.3f}) — {dec.reason}")
        if not dec.place:
            self._journal_log(m, d, dec, side=dec.side or "n/a", fill_price=0.0,
                              contracts=0, edge=0.0, status="skipped")
            return
        if self.frozen:
            return
        if self.execute:
            self._place_maker(m, d, dec, paper=False)
        elif self.live:
            self._place_maker(m, d, dec, paper=True)
        else:
            self._council_ticket(m, d, dec)

    def _journal_log(self, m, d, dec, *, side, fill_price, contracts, edge, status) -> int | None:
        if not self.journal:
            return None
        import statistics as _st
        blind = round(_st.median([e.p_yes for e in d.round1]), 4) if d.round1 else None
        rationale = " | ".join(f"{t.model} {t.p_yes:.2f}" for t in d.round2)
        tid = self.journal.log(market_id=m.id, title=m.title, side=side, converged_p=d.converged_p,
                               blind_p=blind, spread=d.spread, market_price=m.yes_price,
                               executable_price=dec.limit_price_cents / 100.0, edge=edge,
                               contracts=contracts, fill_price=fill_price,
                               fee=kalshi_fee(fill_price, contracts) if contracts and fill_price else 0.0,
                               fill_count=contracts, decision_reason=dec.reason,
                               rationale=rationale, status=status)
        self._mirror(tid)
        return tid

    def _mirror(self, trade_id) -> None:
        if not self.vault_dir:
            return
        from .journal_mirror import mirror_trade
        mirror_trade(self.vault_dir, self.journal.get(trade_id))

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
        if self.live or self.execute:
            self._poll_orders()            # fills/TTL for resting maker orders
        if self.execute:
            self._exit_eval()                  # close positions that hit take-profit / edge-decay
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
            "SELECT DISTINCT market_id FROM trades WHERE status IN ('placed','skipped','working')").fetchall()]

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
                    if self.vault_dir:
                        for r in self.journal._c.execute(
                                "SELECT id FROM trades WHERE market_id=? AND status='resolved'",
                                (mid,)).fetchall():
                            self._mirror(r["id"])
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
