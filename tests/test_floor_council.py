# tests/test_floor_council.py
from council.trading.floor import FloorState
from council.trading.deliberation import Deliberation, ModelEstimate
from council.trading.market import Market
from council.trading.execution import RiskGuard

class FakeCouncil:
    def __init__(self, p, spread): self.p, self.spread = p, spread; self.specs = [1, 2, 3]
    def debate(self, market, notes, lessons=""):
        e = [ModelEstimate("a", self.p, "t"), ModelEstimate("b", self.p, "t")]
        return Deliberation(market.id, e, e, self.p, self.spread, notes)

class FakeResearch:
    def context_for(self, market): return "notes"

class FakeTrader:
    def __init__(self, fill=True): self.orders = []; self.fill = fill
    def place_order(self, ticker, side, count, cents, action="buy"):
        self.orders.append((ticker, side, count, cents))
        n = count if self.fill else 0
        # average_fill_price is YES-terms (sell-YES for a NO order)
        yes_px = cents / 100.0 if side == "yes" else (100 - cents) / 100.0
        return {"fill_count": str(n), "average_fill_price": f"{yes_px:.4f}"}

def _armed_floor(council):
    f = FloorState()
    f.live = True
    f.execute = True
    f.council = council
    f.research = FakeResearch()
    f.trader = FakeTrader()
    f.guard = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)
    f._live_markets = [Market("FED-DEC-CUT", "Fed cuts?", 0.62, yes_bid=0.61, yes_ask=0.63)]
    f.scout = None  # disable scout funnel — these tests exercise the council directly
    return f

def test_council_eval_places_real_order_on_consensus():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))   # 12c NO maker-edge, tight
    f._council_eval()
    # _auto_execute still crosses the real book (taker, hits the bid at 39c) independent of
    # decide()'s maker suggestion (38c = bid+1); full maker-order wiring lands in Task 9.
    assert f.trader.orders == [("FED-DEC-CUT", "no", 8, 39)]   # conviction-scaled size
    assert f.last_debate is not None
    assert f.trades_today == 1

def test_council_eval_skips_on_no_consensus():
    f = _armed_floor(FakeCouncil(p=0.20, spread=0.20))   # huge edge but no consensus
    f._council_eval()
    assert f.trader.orders == []
    assert f.trades_today == 0

def test_daily_trade_cap_halts():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.MAX_TRADES_PER_DAY = 1
    f._council_eval(); f._council_eval()
    assert len(f.trader.orders) == 1

def test_call_cap_disables_live():
    """A council that keeps debating must hit the MAX_LIVE_CALLS backstop and auto-stop."""
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.calls = f.MAX_LIVE_CALLS
    f._council_eval()
    assert f.live is False
    assert f.trader.orders == []      # capped before debating/placing
    assert f.last_debate is None

def test_snapshot_exposes_debate():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f._council_eval()
    snap = f.snapshot()
    assert snap["debate"]["market_id"] == "FED-DEC-CUT"
    assert len(snap["debate"]["round2"]) == 2
    assert snap["debate"]["decision"]["place"] is True

def test_daily_trade_cap_resets_on_new_day():
    from datetime import date, timedelta
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.trades_today = 99
    f._day = date.today() - timedelta(days=1)
    f._council_eval()
    assert f.trades_today == 1            # reset on new day, then +1 for this trade
    assert len(f.trader.orders) == 1

def test_no_fill_books_nothing():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.trader = FakeTrader(fill=False)          # order accepted but 0 fill (didn't cross)
    f._council_eval()
    assert len(f.trader.orders) == 1           # an order WAS attempted
    council = next(b for b in f.snapshot()["books"] if b["key"] == "council")
    assert council["open"] == 0                # but NO phantom position booked
    assert f.trades_today == 0                 # and it doesn't count toward the daily cap

def test_council_paper_fills_when_live_unarmed():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.execute = False                     # LIVE but NOT armed
    f._council_eval()
    assert f.trader.orders == []          # no real order placed
    assert len(f.books["council"]["open"]) == 1
    assert f.trades_today == 0            # paper fills don't consume the real daily cap

def test_floor_logs_trade_on_fill():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f._council_eval()
    rows = f.journal._c.execute("SELECT side,status,fill_count FROM trades").fetchall()
    assert any(r["status"] == "placed" and r["side"] == "no" for r in rows)

def test_resolver_settles_open_trades():
    from council.trading.market import Market as _M
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f._council_eval()                      # logs an open NO trade on FED-DEC-CUT
    class _MD:
        def get_market(self, tk): return _M(tk, "t", 1.0, status="resolved", outcome=1)
    f._market_data = _MD()
    f.resolve_settled()
    row = f.journal._c.execute("SELECT status,outcome FROM trades WHERE status='resolved'").fetchone()
    assert row is not None and row["outcome"] == "yes"

def test_tick_runs_in_worker_thread():
    # B1 regression: the journal is touched from a worker thread (asyncio.to_thread) in prod.
    import asyncio
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f._tick_n = f.DELIBERATE_EVERY - 1            # next tick triggers _council_eval
    asyncio.run(asyncio.to_thread(f.tick))        # pre-fix: sqlite cross-thread error
    assert f.journal._c.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"] >= 1


def test_daily_loss_halts_armed_trading():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.journal.log(market_id="KXOLD-1", title="t", side="no", converged_p=0.5, spread=0.01,
                  market_price=0.5, executable_price=0.5, edge=0.1, contracts=100,
                  fill_price=0.9, fee=0.0, fill_count=100, decision_reason="r", rationale="x")
    f.journal.resolve("KXOLD-1", "yes")           # NO lost -> -$90 realized today
    f._council_eval()
    assert f.trader.orders == []                  # daily-loss cap blocks new armed trades
    assert any("RISK BLOCKED" in a["s"] for a in f.activity)


class FakeMD:
    """Stands in for KalshiMarketData.get_market in exit pricing."""
    def __init__(self, market): self._m = market
    def get_market(self, mid): return self._m if self._m and self._m.id == mid else None

def _armed_floor_with_open_yes():
    f = FloorState()
    f.live = True
    f.execute = True
    # an open YES position: 10 contracts @0.50, council fair value 0.62
    f.journal.log(market_id="Z-1", title="t", side="yes", converged_p=0.62, spread=0.02,
                  market_price=0.50, executable_price=0.52, edge=0.10, contracts=10,
                  fill_price=0.50, fee=0.02, fill_count=10, decision_reason="r",
                  rationale="x", status="placed")
    f.books["council"]["open"].append({"tk": "Z-1", "contracts": 10, "entry": 0.50,
                                        "fee": 0.02, "ttl": 9_999})
    return f

def test_exit_eval_closes_position_on_take_profit():
    f = _armed_floor_with_open_yes()
    # yes bid rose to 0.58 -> gain 0.08 >= default 0.05 take-profit
    f._market_data = FakeMD(Market("Z-1", "t", yes_price=0.59, yes_bid=0.58, yes_ask=0.60))
    f.trader = FakeTrader()                      # fills the sell
    f._exit_eval()
    row = f.journal.get(1)
    assert row["status"] == "exited"
    assert row["realized_pnl"] > 0                       # closed at a profit
    assert f.journal.open_positions() == []
    assert f.books["council"]["open"] == []             # exposure freed

def test_exit_eval_holds_when_no_trigger():
    f = _armed_floor_with_open_yes()
    f._market_data = FakeMD(Market("Z-1", "t", yes_price=0.51, yes_bid=0.50, yes_ask=0.52))
    f.trader = FakeTrader()
    f._exit_eval()
    assert f.journal.open_positions() and f.journal.get(1)["status"] == "placed"

def test_exit_eval_does_not_consult_the_risk_guard():
    # The exit path must never call guard.check (closing only reduces exposure). Make the
    # guard raise if touched, then confirm the exit still completes.
    f = _armed_floor_with_open_yes()
    def _boom(*a, **k): raise AssertionError("exit path must not call guard.check")
    f.guard.check = _boom
    f._market_data = FakeMD(Market("Z-1", "t", yes_price=0.59, yes_bid=0.58, yes_ask=0.60))
    f.trader = FakeTrader()
    f._exit_eval()
    assert f.journal.get(1)["status"] == "exited"               # exit not gated by the guard
