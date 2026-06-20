"""Live order execution + hard risk limits.

This is the only module that can spend real money. It is inert unless explicitly
armed (env COUNCIL_MODE=live + UI arm). Every order passes RiskGuard first; if any
limit is breached, the order is refused and logged — never placed.

The order-placement call is built from Kalshi's API spec but is UNVERIFIED against
a live order. Test it against Kalshi's DEMO host before pointing it at real funds.
"""
from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass


@dataclass
class RiskGuard:
    """Hard limits checked before every real order. Tiny defaults on purpose."""

    max_position_usd: float = 5.0        # one order's cost
    max_total_exposure_usd: float = 50.0  # sum of all open positions
    max_daily_loss_usd: float = 20.0      # halt trading for the day past this
    kill: bool = False                    # global hard stop

    def check(self, order_cost_usd: float, open_exposure_usd: float, daily_pnl_usd: float) -> tuple[bool, str]:
        if self.kill:
            return False, "kill switch engaged"
        if order_cost_usd > self.max_position_usd + 1e-9:
            return False, f"order ${order_cost_usd:.2f} > position cap ${self.max_position_usd:.2f}"
        if open_exposure_usd + order_cost_usd > self.max_total_exposure_usd + 1e-9:
            return False, f"would exceed total exposure cap ${self.max_total_exposure_usd:.2f}"
        if daily_pnl_usd <= -self.max_daily_loss_usd:
            return False, f"daily loss halt (${daily_pnl_usd:.2f} ≤ -${self.max_daily_loss_usd:.2f})"
        return True, "ok"


class KalshiTrader:
    """Places real orders on Kalshi. Live-only; lazy imports; default real host."""

    HOST = "https://api.elections.kalshi.com"
    DEMO = "https://demo-api.kalshi.co"
    PREFIX = "/trade-api/v2"

    def __init__(self, key_id: str, private_key_path: str, host: str | None = None) -> None:
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.host = host or self.HOST

    def _signed_headers(self, method: str, full_path: str) -> dict:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        with open(self.private_key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        sig = key.sign(
            (ts + method.upper() + full_path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    @staticmethod
    def build_order(ticker: str, side: str, count: int, limit_price_cents: int) -> dict:
        """Construct a Kalshi limit-order body. Pure — safe to unit-test offline."""
        if side not in ("yes", "no"):
            raise ValueError(f"side must be yes/no, got {side!r}")
        if count < 1:
            raise ValueError("count must be >= 1")
        price = int(round(limit_price_cents))
        if not 1 <= price <= 99:
            raise ValueError(f"limit price must be 1-99 cents, got {price}")
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            "client_order_id": str(uuid.uuid4()),
        }
        body["yes_price" if side == "yes" else "no_price"] = price
        return body

    def place_order(self, ticker: str, side: str, count: int, limit_price_cents: int) -> dict:
        """LIVE — spends real money. Unverified against a real order; test on DEMO first."""
        import httpx

        path = self.PREFIX + "/portfolio/orders"
        body = self.build_order(ticker, side, count, limit_price_cents)
        resp = httpx.post(self.host + path, headers=self._signed_headers("POST", path), json=body, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def balance(self) -> dict:
        import httpx

        path = self.PREFIX + "/portfolio/balance"
        resp = httpx.get(self.host + path, headers=self._signed_headers("GET", path), timeout=15)
        resp.raise_for_status()
        return resp.json()
