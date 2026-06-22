import time
from council.trading.market import Market


def _m(id="X", price=0.50, volume=1000, close_ts=0, bid=0.0, ask=0.0):
    return Market(id=id, title="t", yes_price=price, volume=volume,
                  close_ts=close_ts, yes_bid=bid, yes_ask=ask)


def test_structural_score_tail_detection():
    """Markets at extreme prices (longshot/near-cert) score higher than mid-priced."""
    from council.trading.scout import structural_score
    mid = structural_score(_m(price=0.50, volume=1000))
    longshot = structural_score(_m(price=0.05, volume=1000))
    near_cert = structural_score(_m(price=0.95, volume=1000))
    assert longshot > mid
    assert near_cert > mid


def test_structural_score_wide_spread():
    """Wide bid/ask spread scores higher than tight spread."""
    from council.trading.scout import structural_score
    tight = structural_score(_m(bid=0.49, ask=0.51))
    wide = structural_score(_m(bid=0.30, ask=0.70))
    assert wide > tight


def test_structural_score_closing_soon():
    """Markets closing in hours score higher than those closing in days."""
    from council.trading.scout import structural_score
    now = int(time.time())
    soon = structural_score(_m(close_ts=now + 3600))       # 1 hour
    later = structural_score(_m(close_ts=now + 86400 * 7)) # 7 days
    assert soon > later


def test_structural_score_stale_price_high_volume():
    """High volume + mid price (not moved) scores higher than low volume."""
    from council.trading.scout import structural_score
    stale_busy = structural_score(_m(price=0.50, volume=50000))
    stale_quiet = structural_score(_m(price=0.50, volume=500))
    assert stale_busy > stale_quiet


def test_shortlist_returns_top_n():
    """shortlist(n=3) on 5 markets returns 3, best structural_score first."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    now = int(time.time())
    markets = [
        _m(id="A", price=0.50, volume=100, close_ts=now + 86400 * 7),
        _m(id="B", price=0.05, volume=1000, close_ts=now + 3600),       # longshot + closing soon
        _m(id="C", price=0.95, volume=2000, close_ts=now + 7200),       # near-cert + high vol
        _m(id="D", price=0.50, volume=200, close_ts=now + 86400),
        _m(id="E", price=0.50, volume=500, close_ts=now + 86400 * 3),
    ]
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=3, max_escalate=1)
    result = scout.shortlist(markets)
    assert len(result) == 3
    ids = [m.id for m in result]
    # B and C should be in top 3 (tail + urgency); A should NOT be (boring mid-price, far out)
    assert "B" in ids and "C" in ids
    assert "A" not in ids


def test_shortlist_empty_pool():
    """shortlist([]) returns []."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=8, max_escalate=1)
    assert scout.shortlist([]) == []


def test_shortlist_fewer_than_n():
    """When pool < shortlist_n, return all markets."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    markets = [_m(id="A"), _m(id="B")]
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.shortlist(markets)
    assert len(result) == 2
