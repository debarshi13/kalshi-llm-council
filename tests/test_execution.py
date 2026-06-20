import pytest

from council.trading.book import Estimate
from council.trading.deliberation import Deliberation, ModelEstimate
from council.trading.execution import KalshiTrader, RiskGuard
from council.trading.floor import FloorState


# --- order construction (pure, no network) ---------------------------------
def test_build_order_yes():
    b = KalshiTrader.build_order("FED-X", "yes", 10, 62)
    assert b["ticker"] == "FED-X" and b["side"] == "yes" and b["count"] == 10
    assert b["action"] == "buy" and b["type"] == "limit" and b["yes_price"] == 62
    assert "yes_price" in b and "no_price" not in b and "client_order_id" in b


def test_build_order_no():
    b = KalshiTrader.build_order("X", "no", 5, 59)
    assert b["no_price"] == 59 and "yes_price" not in b


def test_build_order_rejects_bad_inputs():
    for price in (0, 100, -3):
        with pytest.raises(ValueError):
            KalshiTrader.build_order("X", "yes", 1, price)
    with pytest.raises(ValueError):
        KalshiTrader.build_order("X", "yes", 0, 50)
    with pytest.raises(ValueError):
        KalshiTrader.build_order("X", "maybe", 1, 50)


# --- risk guard -------------------------------------------------------------
def test_risk_guard_limits():
    g = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)
    assert g.check(4, 0, 0)[0] is True
    assert g.check(6, 0, 0)[0] is False        # position cap
    assert g.check(4, 48, 0)[0] is False       # total exposure cap
    assert g.check(4, 0, -20)[0] is False      # daily-loss halt
    g.kill = True
    assert g.check(1, 0, 0)[0] is False         # kill switch


# --- floor auto-execution (fake trader: NO real orders) ---------------------
class _FakeTrader:
    def __init__(self):
        self.placed = []

    def place_order(self, ticker, side, count, price):
        self.placed.append((ticker, side, count, price))
        return {"order": {"status": "resting"}}


class _FakeCouncil:
    """Fake council that always returns a high-conviction consensus."""
    def debate(self, market, notes):
        e = [ModelEstimate("a", 0.95, "high conviction"), ModelEstimate("b", 0.95, "high conviction")]
        return Deliberation(market.id, e, e, 0.95, 0.01, notes)

class _FakeResearch:
    def context_for(self, market): return "notes"


def _armed_floor(guard):
    f = FloorState()
    f.council = _FakeCouncil()
    f.research = _FakeResearch()
    f._live_markets = f.markets
    f.live = True
    f.auto = True
    f.execute = True
    f.trader = _FakeTrader()
    f.guard = guard
    return f


def test_auto_execute_places_orders_no_approval():
    f = _armed_floor(RiskGuard(max_position_usd=100, max_total_exposure_usd=1000, max_daily_loss_usd=100))
    for _ in range(f.DELIBERATE_EVERY * 3):
        f.tick()
    assert f.trader.placed, "expected real orders to be placed"
    assert f.snapshot()["execute"] is True
    assert f.snapshot()["tickets"] == []        # autonomous — nothing waits for approval


def test_risk_caps_block_every_order():
    f = _armed_floor(RiskGuard(max_position_usd=0.01, max_total_exposure_usd=0.01, max_daily_loss_usd=20))
    for _ in range(f.DELIBERATE_EVERY * 3):
        f.tick()
    assert f.trader.placed == [], "risk caps should have blocked all orders"
    assert any("RISK BLOCKED" in a["s"] for a in f.activity)


def test_kill_switch_halts_execution():
    f = _armed_floor(RiskGuard(max_position_usd=100, max_total_exposure_usd=1000, max_daily_loss_usd=100))
    f.frozen = True
    for _ in range(f.DELIBERATE_EVERY * 3):
        f.tick()
    assert f.trader.placed == []                 # frozen = no new orders


def test_arm_requires_live_mode(monkeypatch):
    monkeypatch.delenv("COUNCIL_MODE", raising=False)
    with pytest.raises(RuntimeError, match="COUNCIL_MODE"):
        FloorState().arm_execution()
