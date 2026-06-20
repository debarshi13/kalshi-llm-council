# tests/test_floor_council.py
from council.trading.floor import FloorState
from council.trading.deliberation import Deliberation, ModelEstimate
from council.trading.market import Market
from council.trading.execution import RiskGuard

class FakeCouncil:
    def __init__(self, p, spread): self.p, self.spread = p, spread; self.specs = [1, 2, 3]
    def debate(self, market, notes):
        e = [ModelEstimate("a", self.p, "t"), ModelEstimate("b", self.p, "t")]
        return Deliberation(market.id, e, e, self.p, self.spread, notes)

class FakeResearch:
    def context_for(self, market): return "notes"

class FakeTrader:
    def __init__(self): self.orders = []
    def place_order(self, ticker, side, count, cents): self.orders.append((ticker, side, count, cents))

def _armed_floor(council):
    f = FloorState()
    f.live = True
    f.execute = True
    f.council = council
    f.research = FakeResearch()
    f.trader = FakeTrader()
    f.guard = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)
    f._live_markets = [Market("FED-DEC-CUT", "Fed cuts?", 0.62)]
    return f

def test_council_eval_places_real_order_on_consensus():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))   # 12c NO edge, tight
    f._council_eval()
    assert f.trader.orders == [("FED-DEC-CUT", "no", 7, 38)]   # conviction-scaled size
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

def test_council_paper_fills_when_live_unarmed():
    f = _armed_floor(FakeCouncil(p=0.50, spread=0.01))
    f.execute = False                     # LIVE but NOT armed
    f._council_eval()
    assert f.trader.orders == []          # no real order placed
    assert len(f.books["council"]["open"]) == 1
    assert f.trades_today == 1
