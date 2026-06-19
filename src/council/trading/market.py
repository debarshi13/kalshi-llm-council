"""Market data: a provider-agnostic interface, an offline mock, and a Kalshi adapter.

Prices are YES-contract prices in dollars, 0.0–1.0 (a contract pays $1 if the
event resolves YES). The council reads markets through the `MarketData`
protocol so the paper harness runs offline against `MockMarketData` and swaps to
`KalshiMarketData` for live prices with no change to the strategy code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Market:
    id: str
    title: str
    yes_price: float          # 0.0–1.0, dollars; NO price is 1 - yes_price
    volume: int = 0
    status: str = "open"      # "open" | "resolved"
    outcome: int | None = None  # 1 (YES) | 0 (NO) once resolved

    def __post_init__(self) -> None:
        if not 0.0 <= self.yes_price <= 1.0:
            raise ValueError(f"yes_price must be in [0,1], got {self.yes_price}")

    @property
    def no_price(self) -> float:
        return round(1.0 - self.yes_price, 4)

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.outcome is not None


class MarketData(Protocol):
    def list_markets(self) -> list[Market]: ...
    def get_market(self, market_id: str) -> Market | None: ...


class MockMarketData:
    """Deterministic in-memory markets for offline dev and tests.

    Lets a test set prices and resolve outcomes to exercise the P&L/scoring path.
    """

    def __init__(self, markets: list[Market] | None = None) -> None:
        self._markets: dict[str, Market] = {m.id: m for m in (markets or _DEFAULT_MARKETS())}

    def list_markets(self) -> list[Market]:
        return list(self._markets.values())

    def get_market(self, market_id: str) -> Market | None:
        return self._markets.get(market_id)

    # --- test/sim helpers ---------------------------------------------------
    def set_price(self, market_id: str, yes_price: float) -> None:
        self._markets[market_id].yes_price = yes_price

    def resolve(self, market_id: str, outcome: int) -> None:
        m = self._markets[market_id]
        m.status = "resolved"
        m.outcome = outcome


def _DEFAULT_MARKETS() -> list[Market]:
    return [
        Market("FED-DEC-CUT", "Fed cuts rates in December?", 0.62, volume=120_000),
        Market("CPI-NOV-HOT", "November CPI above 3.2%?", 0.41, volume=45_000),
        Market("GOVT-SHUTDOWN", "US govt shutdown before year end?", 0.18, volume=8_000),
    ]


class KalshiMarketData:
    """Live Kalshi adapter. Read-only endpoints (markets, prices).

    Live-only and unverified offline. Requires the optional `kalshi` extra
    (`httpx`, `cryptography`) and RSA request signing per Kalshi's API. Imports
    are lazy so the rest of the package never depends on them.
    """

    BASE = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, key_id: str, private_key_path: str, base_url: str | None = None) -> None:
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.base = base_url or self.BASE

    def _signed_headers(self, method: str, path: str) -> dict:
        import base64
        import time

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        with open(self.private_key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        msg = (ts + method.upper() + path).encode()
        sig = key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _get(self, path: str) -> dict:
        import httpx

        headers = self._signed_headers("GET", path)
        resp = httpx.get(self.base + path, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def list_markets(self) -> list[Market]:
        data = self._get("/markets?status=open&limit=100")
        return [self._to_market(m) for m in data.get("markets", [])]

    def get_market(self, market_id: str) -> Market | None:
        data = self._get(f"/markets/{market_id}")
        m = data.get("market")
        return self._to_market(m) if m else None

    @staticmethod
    def _to_market(m: dict) -> Market:
        # Kalshi quotes cents (0–100); convert to dollars.
        yes = (m.get("yes_bid", 0) or 0) / 100.0
        status = "resolved" if m.get("status") == "settled" else "open"
        outcome = {"yes": 1, "no": 0}.get(m.get("result", ""))
        return Market(
            id=m.get("ticker", m.get("id", "?")),
            title=m.get("title", ""),
            yes_price=min(max(yes, 0.0), 1.0),
            volume=int(m.get("volume", 0) or 0),
            status=status,
            outcome=outcome,
        )
