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
    def place_order(self, ticker, side, count, cents):
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
    f._live_markets = [Market("FED-DEC-CUT", "Fed cuts?", 0.62)]
    f.scout = None  # disable scout funnel — these tests exercise the council directly
    return f

def test_council_eval_places_real_order_on_consensus():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))   # 12c NO edge, tight
    f._council_eval()
    assert f.trader.orders == [("FED-DEC-CUT", "no", 8, 38)]   # conviction-scaled size (3c daytrade gate)
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
