from council.trading.deliberation import Deliberation, ModelEstimate, Decision, decide
from council.trading.execution import RiskGuard
from council.trading.market import Market

CAPS = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)

def _delib(market_id, converged_p, spread):
    return Deliberation(market_id, [], [], converged_p, spread, "")

def test_consensus_place_no_side():
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = _delib(m.id, 0.533, 0.018)          # mean below price -> NO; tight spread
    out = decide(d, m, CAPS)
    assert out.place is True
    assert out.side == "no"
    # NO entry = 1 - 0.62 = 0.38 -> 38c; contracts = floor(5/0.38)=13
    assert out.limit_price_cents == 38
    assert out.contracts == 13

def test_consensus_place_yes_side():
    m = Market("CPI-NOV-HOT", "CPI hot?", 0.41)
    d = _delib(m.id, 0.52, 0.02)            # mean above price -> YES, 11c edge
    out = decide(d, m, CAPS)
    assert out.place is True and out.side == "yes"
    assert out.limit_price_cents == 41

def test_high_spread_skips():
    m = Market("X", "x?", 0.50)
    d = _delib(m.id, 0.70, 0.12)            # big edge but no consensus
    out = decide(d, m, CAPS)
    assert out.place is False and "consensus" in out.reason.lower()

def test_subthreshold_edge_skips():
    m = Market("X", "x?", 0.50)
    d = _delib(m.id, 0.53, 0.01)            # 3c edge < 6c
    out = decide(d, m, CAPS)
    assert out.place is False and "threshold" in out.reason.lower()

def test_price_clamped_to_valid_range():
    m = Market("X", "x?", 0.95)
    d = _delib(m.id, 0.99, 0.0)             # YES, entry 0.95 -> 95c valid
    out = decide(d, m, CAPS)
    assert 1 <= out.limit_price_cents <= 99
