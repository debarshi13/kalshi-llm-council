import pytest

from council.trading.book import Estimate
from council.trading.deliberation import Deliberation, ModelEstimate
from council.trading.execution import KalshiTrader, RiskGuard
from council.trading.floor import FloorState


# --- order construction (pure, no network) ---------------------------------
def test_build_order_yes_is_bid_in_dollars():
    b = KalshiTrader.build_order("FED-X", "yes", 10, 62)
    assert b["ticker"] == "FED-X"
    assert b["side"] == "bid"            # buy YES == bid on the YES leg (V2)
    assert b["count"] == "10.00"         # fixed-point string
    assert b["price"] == "0.6200"        # dollars, not cents
    assert b["time_in_force"] and b["self_trade_prevention_type"]
    assert "client_order_id" in b and "yes_price" not in b


def test_build_order_no_is_ask_at_complement():
    b = KalshiTrader.build_order("X", "no", 5, 59)   # buy NO @59c == sell YES @41c
    assert b["side"] == "ask" and b["price"] == "0.4100" and b["count"] == "5.00"


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
        yes_px = price / 100.0 if side == "yes" else (100 - price) / 100.0
        return {"fill_count": str(count), "average_fill_price": f"{yes_px:.4f}"}


class _FakeCouncil:
    """Fake council that always returns a high-conviction consensus."""
    specs = [1, 2, 3]
    def debate(self, market, notes, lessons=""):
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
    f.scout = None  # disable scout funnel — these tests exercise execution directly
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


# --- new sell/close action path (Task 3) ------------------------------------
def test_build_order_buy_is_unchanged_no_action_key():
    b = KalshiTrader.build_order("X", "yes", 10, 62)        # default action="buy"
    assert b["side"] == "bid" and b["price"] == "0.6200"
    assert "action" not in b                                # buy body must stay byte-identical


def test_build_order_sell_yes_carries_action_sell():
    b = KalshiTrader.build_order("X", "yes", 10, 55, action="sell")
    assert b["action"] == "sell"
    assert b["side"] == "bid" and b["price"] == "0.5500"    # sell yes -> hit the yes bid
    assert b["time_in_force"] == "immediate_or_cancel"


def test_build_order_sell_no_carries_action_sell():
    b = KalshiTrader.build_order("X", "no", 5, 40, action="sell")  # sell no @40c
    assert b["action"] == "sell" and b["side"] == "ask" and b["price"] == "0.6000"


def test_build_order_rejects_bad_action():
    with pytest.raises(ValueError):
        KalshiTrader.build_order("X", "yes", 1, 50, action="hold")
