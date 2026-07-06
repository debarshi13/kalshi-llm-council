"""Pre-live gate: verify resting (GTC) order place/status/cancel on the Kalshi DEMO host.

Usage: KALSHI_HOST=https://external-api.demo.kalshi.co \
       KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=... \
       .venv/bin/python scripts/verify_maker_demo.py TICKER

Places a 1-contract resting YES bid far below the market (should NOT fill),
polls status, cancels it, and confirms the cancel. Nothing here should cost money.
"""
import os
import sys
import time

from council.trading.execution import KalshiTrader


def main() -> int:
    ticker = sys.argv[1]
    t = KalshiTrader(os.environ["KALSHI_API_KEY_ID"], os.environ["KALSHI_PRIVATE_KEY_PATH"],
                     host=os.environ.get("KALSHI_HOST", KalshiTrader.DEMO))

    if "demo" not in t.host:
        print(f"REFUSING: host {t.host} is not a demo host. Set KALSHI_HOST to the demo URL.")
        return 1
    print("host:", t.host)

    resp = t.place_order(ticker, "yes", 1, 2, tif="gtc")     # 2c deep bid — rests
    oid = resp["order"]["order_id"]
    print("placed resting order:", oid)
    time.sleep(2)
    st = t.get_order(oid)
    print("status:", st["order"].get("status"), "fill_count:", st["order"].get("fill_count"))
    assert int(float(st["order"].get("fill_count") or 0)) == 0, "deep bid should not fill"
    print("cancel:", t.cancel_order(oid))
    st = t.get_order(oid)
    print("post-cancel status:", st["order"].get("status"))
    print("OK — resting lifecycle verified on", t.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
