import pytest

from council.trading.ledger import InsufficientCash, PaperLedger, kalshi_fee
from council.trading.market import Market, MockMarketData
from council.trading.risk import RiskCaps


# --- fee model -------------------------------------------------------------
def test_kalshi_fee_formula():
    # ceil(0.07 * 100 * 0.5 * 0.5 * 100)/100 = 1.75
    assert kalshi_fee(0.5, 100) == 1.75
    # rounds UP to the next cent
    assert kalshi_fee(0.62, 10) == 0.17
    # YES/NO symmetric (p(1-p))
    assert kalshi_fee(0.3, 50) == kalshi_fee(0.7, 50)


# --- market ----------------------------------------------------------------
def test_mock_market_resolve():
    md = MockMarketData()
    m = md.get_market("FED-DEC-CUT")
    assert m and abs(m.no_price - (1 - m.yes_price)) < 1e-9
    md.resolve("FED-DEC-CUT", 1)
    assert md.get_market("FED-DEC-CUT").resolved


def test_bad_price_rejected():
    with pytest.raises(ValueError):
        Market("X", "bad", 1.5)


# --- ledger ----------------------------------------------------------------
def test_open_deducts_cash_and_fee():
    led = PaperLedger(1000.0, book="A")
    m = Market("M", "t", 0.62)
    t = led.open_trade(m, "yes", 10, predicted_prob=0.8)
    assert t.fee == 0.17
    assert round(led.cash, 2) == round(1000 - (10 * 0.62 + 0.17), 2)
    assert led.open_exposure == 6.2


def test_winning_trade_pnl_and_calibration():
    led = PaperLedger(1000.0)
    m = Market("M", "t", 0.62)
    t = led.open_trade(m, "yes", 10, predicted_prob=0.8)
    pnl = led.resolve_trade(t, outcome=1)
    assert pnl == round(10 - 6.2 - 0.17, 4)  # 3.63
    s = led.score()
    assert s["hit_rate"] == 1.0
    assert s["brier"] == round((0.8 - 1) ** 2, 4)          # 0.04
    assert s["market_brier"] == round((0.62 - 1) ** 2, 4)  # 0.1444
    assert s["beats_market"] is True


def test_losing_trade_pnl():
    led = PaperLedger(1000.0)
    m = Market("M", "t", 0.62)
    t = led.open_trade(m, "yes", 10, predicted_prob=0.8)
    pnl = led.resolve_trade(t, outcome=0)
    assert pnl == round(-6.2 - 0.17, 4)


def test_insufficient_cash():
    led = PaperLedger(1.0)
    with pytest.raises(InsufficientCash):
        led.open_trade(Market("M", "t", 0.62), "yes", 100, predicted_prob=0.5)


# --- risk caps -------------------------------------------------------------
def test_risk_position_cap():
    led = PaperLedger(1000.0)
    caps = RiskCaps(bankroll=1000.0, max_position_frac=0.05)  # cap = $50
    assert caps.check(led, proposed_cost=60.0, today_realized_pnl=0.0).allowed is False
    assert caps.check(led, proposed_cost=40.0, today_realized_pnl=0.0).allowed is True


def test_risk_daily_loss_halt():
    led = PaperLedger(1000.0)
    caps = RiskCaps(bankroll=1000.0, daily_loss_frac=0.10)  # halt at -$100
    assert caps.check(led, proposed_cost=10.0, today_realized_pnl=-100.0).allowed is False


def test_risk_kill_switch_and_cash():
    led = PaperLedger(20.0)
    caps = RiskCaps(bankroll=1000.0, kill_switch=True)
    assert caps.check(led, 10.0, 0.0).allowed is False  # kill switch
    caps.kill_switch = False
    # within position cap (5% of 1000 = 50) but exceeds the $20 cash on hand
    assert caps.check(led, 30.0, 0.0).allowed is False


# --- book loop -------------------------------------------------------------
def _book(beliefs=None, bankroll=1000.0, kill=False):
    from council.trading.book import Book, MockAnalyst
    return Book(
        name="A",
        analyst=MockAnalyst(beliefs),
        ledger=PaperLedger(bankroll, book="A"),
        risk=RiskCaps(bankroll=bankroll, max_position_frac=0.05, kill_switch=kill),
    )


def test_book_no_edge_no_trades():
    md = MockMarketData()
    book = _book()  # default analyst = market price → zero edge
    assert book.run_tick(md, ["FED-DEC-CUT", "CPI-NOV-HOT"]) == []


def test_book_opens_yes_on_positive_edge_and_profits():
    md = MockMarketData()
    book = _book(beliefs={"FED-DEC-CUT": 0.85})  # market 0.62 → +0.23 edge
    opened = book.run_tick(md, ["FED-DEC-CUT"])
    assert len(opened) == 1 and opened[0].side == "yes"

    md.resolve("FED-DEC-CUT", 1)
    book.ledger.resolve_trade(opened[0], 1)
    s = book.ledger.score()
    assert s["realized_pnl"] > 0
    assert s["beats_market"] is True  # 0.85 calibrated closer to YES than 0.62


def test_book_opens_no_on_negative_edge():
    md = MockMarketData()
    book = _book(beliefs={"CPI-NOV-HOT": 0.20})  # market 0.41 → -0.21 edge → NO
    opened = book.run_tick(md, ["CPI-NOV-HOT"])
    assert len(opened) == 1 and opened[0].side == "no"


def test_book_kill_switch_blocks_all():
    md = MockMarketData()
    book = _book(beliefs={"FED-DEC-CUT": 0.85}, kill=True)
    assert book.run_tick(md, ["FED-DEC-CUT"]) == []


def test_to_market_parses_resolution_rules():
    from council.trading.market import KalshiMarketData
    m = KalshiMarketData._to_market({
        "ticker": "X", "title": "t", "yes_bid_dollars": "0.58", "yes_ask_dollars": "0.60",
        "volume_fp": "100", "rules_primary": "Resolves YES if core PCE is above 0.2%.",
        "rules_secondary": "Per the BEA monthly release.",
    })
    assert "above 0.2%" in m.rules and "BEA" in m.rules
    assert KalshiMarketData._to_market({"ticker": "Y", "title": "t"}).rules == ""
