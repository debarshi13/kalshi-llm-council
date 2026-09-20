"""Acceptance-only probe for the action='sell' order body (PRODUCTION).

Places ONE 1-contract sell priced so it cannot cross (no bid that high), so it
IOC-cancels with no fill: $0 cost, no position opened. The point is to prove
Kalshi ACCEPTS the sell body (HTTP 200) rather than rejecting it as malformed
(HTTP 400). Whether a sell *fills* is liquidity, and the fill/P&L parsing reuses
the already-proven buy path.

Run from the repo root:  .venv/bin/python scripts/probe_sell_accept.py [TICKER] [PRICE_CENTS]
Defaults: TICKER=KXT20MATCH-26JUN281730SEALOS-SEA  PRICE_CENTS=99
"""
import sys
import os
import pathlib
import httpx
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")
from council.trading.execution import KalshiTrader

ticker = sys.argv[1] if len(sys.argv) > 1 else "KXT20MATCH-26JUN281730SEALOS-SEA"
price = int(sys.argv[2]) if len(sys.argv) > 2 else 99   # 99c sell can't cross a normal book

t = KalshiTrader(os.environ["KALSHI_API_KEY_ID"], os.environ["KALSHI_PRIVATE_KEY_PATH"])

print(f"balance before: ${t.balance().get('balance_dollars')}")
print(f"probe: SELL 1 yes @ {price}c on {ticker} (action='sell', non-crossing)\n")
try:
    resp = t.place_order(ticker, "yes", 1, price, action="sell")
    fc = int(float(resp.get("fill_count", 0) or 0))
    print("HTTP 200 — body ACCEPTED ✅")
    print("  status:", resp.get("status"), "| fill_count:", fc, "| order_id:", resp.get("order_id"))
    if fc >= 1:
        print("  ⚠ UNEXPECTEDLY FILLED — you now hold a 1-contract short on this market; "
              "close it by BUYING 1 yes back.")
    else:
        print("  no fill (as intended) — no position opened, $0 cost.")
    print("\nRESULT: the action='sell' order body is accepted by Kalshi. ✅")
except httpx.HTTPStatusError as e:
    print(f"HTTP {e.response.status_code} — body REJECTED ❌")
    print("  response:", e.response.text[:500])
    print("\nRESULT: the sell body needs fixing before live exits. Capture the above for the fix.")
except Exception as e:  # noqa: BLE001
    print(f"ERROR: {type(e).__name__}: {e}")
print(f"\nbalance after: ${t.balance().get('balance_dollars')}")
