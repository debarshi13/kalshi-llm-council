import time
import math
from council.trading.market import Market
from council.trading.selection import SelectionParams, edge_score, maker_price_cents


def _mk(price, volume, hrs=48.0, bid=None, ask=None):
    return Market("KXTEST-A", "t", price, volume=volume,
                  close_ts=int(time.time() + hrs * 3600),
                  yes_bid=bid or 0.0, yes_ask=ask or 0.0)


def test_tail_beats_midprice_all_else_equal():
    assert edge_score(_mk(0.90, 500)) > edge_score(_mk(0.50, 500))


def test_underfollowed_beats_heavy_volume():
    assert edge_score(_mk(0.90, 500)) > edge_score(_mk(0.90, 100_000))


def test_below_min_volume_is_untradeable():
    assert edge_score(_mk(0.90, 10)) == -math.inf


def test_subhour_lottery_excluded():
    assert edge_score(_mk(0.90, 500, hrs=0.5)) == -math.inf


def test_beyond_horizon_excluded():
    assert edge_score(_mk(0.90, 500, hrs=24 * 30)) == -math.inf


def test_maker_viable_spread_beats_no_book():
    with_book = _mk(0.90, 500, bid=0.87, ask=0.93)
    without = _mk(0.90, 500)
    assert edge_score(with_book) > edge_score(without)


def test_maker_price_sits_inside_spread():
    m = _mk(0.90, 500, bid=0.87, ask=0.93)
    assert maker_price_cents(m, "yes") == 88          # bid+1c
    assert maker_price_cents(m, "no") == 100 - 92     # NO resting = 1 - (ask-1c)


def test_maker_price_none_without_book():
    assert maker_price_cents(_mk(0.90, 500), "yes") is None
