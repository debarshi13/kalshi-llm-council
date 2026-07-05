from council.trading.ledger import kalshi_fee, maker_fee, required_edge


def test_maker_fee_is_quarter_of_taker_before_rounding():
    # 10 contracts at 50c: taker raw = 17.5c -> maker raw = 4.375c -> ceil 5c
    assert maker_fee(0.50, 10) == 0.05
    # 10 contracts at 90c: taker raw = 6.3c -> maker raw = 1.575c -> ceil 2c
    assert maker_fee(0.90, 10) == 0.02


def test_required_edge_cheaper_in_tails_than_at_midprice():
    mid = required_edge(0.50)
    tail = required_edge(0.90)
    assert tail < mid
    # floor: always at least spread_buffer + min_profit
    assert tail >= 0.02


def test_required_edge_components():
    # at 0.90, 10 contracts: per-contract maker fee = 0.02/10 = 0.002
    assert abs(required_edge(0.90, 10, 0.01, 0.01) - (0.002 + 0.01 + 0.01)) < 1e-9
