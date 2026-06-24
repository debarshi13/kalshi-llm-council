"""Round-trip a 1-contract YES position on the Kalshi DEMO exchange to prove the
action='sell' close body is accepted and fills. Run before enabling live exits.

Env: KALSHI_DEMO_API_KEY_ID, KALSHI_DEMO_PRIVATE_KEY_PATH, and TICKER (a liquid demo market).
"""
import os, sys
from council.trading.execution import KalshiTrader

kid = os.environ.get("KALSHI_DEMO_API_KEY_ID")
pk = os.environ.get("KALSHI_DEMO_PRIVATE_KEY_PATH")
ticker = os.environ.get("TICKER")
if not (kid and pk and ticker):
    sys.exit("set KALSHI_DEMO_API_KEY_ID, KALSHI_DEMO_PRIVATE_KEY_PATH, TICKER")

t = KalshiTrader(kid, pk, host=KalshiTrader.DEMO)
print("buy  ->", t.place_order(ticker, "yes", 1, 60, action="buy"))
print("sell ->", t.place_order(ticker, "yes", 1, 40, action="sell"))  # close: cross down into the bid
