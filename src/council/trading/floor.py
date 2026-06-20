"""Live, monitorable paper floor — the state the web UI polls.

A continuously-ticking simulation of the three books finding edges, queuing
trades for human approval, and resolving positions into P&L. It is deliberately
**mock** (random beliefs, no model calls) so it can run forever at zero cost
while you watch and approve. Swap the estimator for the LiteLLM analysts when
you're ready to spend tokens on real reasoning.
"""
from __future__ import annotations

import itertools
import random
import time

from .ledger import kalshi_fee
from .market import MockMarketData

# (book id, model name, key, skill = P(a shipped trade resolves in our favor))
BOOKS = [("A", "Claude", "claude", 0.56), ("B", "Kimi K2", "kimi", 0.61), ("C", "GLM-5.2", "glm", 0.43)]


class FloorState:
    EDGE = 0.06          # min edge to raise a ticket
    MAX_PENDING = 2      # per book

    def __init__(self) -> None:
        self.markets = MockMarketData().list_markets()
        self.books = {
            key: {"book": b, "name": name, "key": key, "skill": skill,
                  "pnl": 0.0, "open": [], "wins": 0, "trades": 0}
            for b, name, key, skill in BOOKS
        }
        self.output = 0.0
        self.tickets: list[dict] = []
        self.activity: list[dict] = []
        self.frozen = False
        self._ids = itertools.count(1)

    def tick(self) -> None:
        if self.frozen:
            return
        for key, bk in self.books.items():
            if sum(1 for t in self.tickets if t["key"] == key) >= self.MAX_PENDING:
                continue
            if random.random() < 0.45:        # not every book acts every tick
                continue
            m = random.choice(self.markets)
            prob = min(max(m.yes_price + random.gauss(0, 0.14), 0.02), 0.98)
            edge = prob - m.yes_price
            if abs(edge) < self.EDGE:
                continue
            side = "yes" if edge > 0 else "no"
            entry = m.yes_price if side == "yes" else round(1 - m.yes_price, 2)
            contracts = max(1, int(50 / max(entry, 0.05)))
            self.tickets.append({
                "id": next(self._ids), "key": key, "book": bk["book"], "who": f"{bk['name']} · {bk['book']}",
                "tk": m.id, "ti": m.title, "side": side, "prob": round(prob, 2),
                "price": round(m.yes_price, 2), "contracts": contracts, "entry": entry,
            })
            self._log(f"{bk['name']} found a {abs(edge)*100:.0f}¢ edge on {m.id} → wants {side.upper()}")

        # resolve held positions over time so P&L actually moves
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

    def approve(self, ticket_id: int, granted: bool) -> bool:
        t = next((x for x in self.tickets if x["id"] == ticket_id), None)
        if not t:
            return False
        self.tickets.remove(t)
        bk = self.books[t["key"]]
        if granted:
            bk["open"].append({"tk": t["tk"], "contracts": t["contracts"], "entry": t["entry"],
                               "fee": kalshi_fee(t["entry"], t["contracts"]), "ttl": random.randint(2, 5)})
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
            "books": [
                {"book": bk["book"], "name": bk["name"], "key": k, "pnl": round(bk["pnl"], 2),
                 "open": len(bk["open"]), "trades": bk["trades"],
                 "hit": round(bk["wins"] / bk["trades"] * 100) if bk["trades"] else None}
                for k, bk in self.books.items()
            ],
            "tickets": self.tickets,
            "activity": self.activity[:14],
        }
