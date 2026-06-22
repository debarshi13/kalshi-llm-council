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
