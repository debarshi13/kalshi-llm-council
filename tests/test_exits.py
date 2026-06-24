import pytest
from council.trading.market import Market
from council.trading.exits import OpenPosition, ExitParams, ExitDecision, exit_signal

def _pos(side="yes", entry=0.40, fair=0.55, contracts=10):
    return OpenPosition(trade_id=1, market_id="X-1", side=side, entry_price=entry,
                        fair_value=fair, contracts=contracts, entry_fee=0.01)

def _mkt(yes_bid=0.40, yes_ask=0.42):
    return Market("X-1", "t", yes_price=(yes_bid + yes_ask) / 2, yes_bid=yes_bid, yes_ask=yes_ask)

P = ExitParams(take_profit=0.05, exit_edge=0.01, stop_loss=None)

def test_hold_when_neither_trigger_fires():
    # bought yes @0.40, fair 0.55, market still ~0.42 -> gain 0.02 (<0.05), edge 0.13 (>0.01)
    d = exit_signal(_pos(), _mkt(yes_bid=0.42, yes_ask=0.44), P)
    assert d.should_exit is False

def test_take_profit_fires_on_favorable_move():
    # yes bid rose to 0.46 -> gain 0.06 >= 0.05
    d = exit_signal(_pos(entry=0.40), _mkt(yes_bid=0.46, yes_ask=0.48), P)
    assert d.should_exit is True and "take-profit" in d.reason and d.exit_price == 0.46

def test_edge_decay_fires_when_market_reaches_fair_value():
    # fair 0.55, yes bid 0.55 -> remaining edge 0.00 <= 0.01. Entry high so TP does NOT fire first.
    d = exit_signal(_pos(entry=0.52, fair=0.55), _mkt(yes_bid=0.55, yes_ask=0.57), P)
    assert d.should_exit is True and "edge" in d.reason

def test_no_side_take_profit_uses_no_bid():
    # short yes / long no: entry no-price 0.40 (yes 0.60), fair_value(yes)=0.45 -> fair(no)=0.55.
    # yes_ask 0.54 -> no_bid = 1-0.54 = 0.46 -> gain 0.06 >= 0.05.
    d = exit_signal(_pos(side="no", entry=0.40, fair=0.45), _mkt(yes_bid=0.52, yes_ask=0.54), P)
    assert d.should_exit is True and "take-profit" in d.reason and d.exit_price == 0.46

def test_stop_loss_off_by_default_holds_a_loser():
    # bought yes @0.40, bid dropped to 0.30 -> down 0.10, but stop_loss None and edge grew -> HOLD
    d = exit_signal(_pos(entry=0.40, fair=0.55), _mkt(yes_bid=0.30, yes_ask=0.32), P)
    assert d.should_exit is False

def test_stop_loss_fires_when_enabled():
    params = ExitParams(take_profit=0.05, exit_edge=0.01, stop_loss=0.08)
    d = exit_signal(_pos(entry=0.40, fair=0.55), _mkt(yes_bid=0.30, yes_ask=0.32), params)
    assert d.should_exit is True and "stop-loss" in d.reason

def test_missing_quote_holds():
    d = exit_signal(_pos(), _mkt(yes_bid=0.0, yes_ask=0.0), P)
    assert d.should_exit is False
