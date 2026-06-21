"""Market data: a provider-agnostic interface, an offline mock, and a Kalshi adapter.

Prices are YES-contract prices in dollars, 0.0–1.0 (a contract pays $1 if the
event resolves YES). The council reads markets through the `MarketData`
protocol so the paper harness runs offline against `MockMarketData` and swaps to
`KalshiMarketData` for live prices with no change to the strategy code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class Market:
    id: str
    title: str
    yes_price: float          # 0.0–1.0, dollars; NO price is 1 - yes_price
    volume: int = 0
    status: str = "open"      # "open" | "resolved"
    outcome: int | None = None  # 1 (YES) | 0 (NO) once resolved
    close_ts: int = 0         # unix epoch the market closes (0 = unknown)
    yes_bid: float = 0.0      # best bid / ask on the YES leg (for crossing the spread)
    yes_ask: float = 0.0
    rules: str = ""           # resolution criteria (how the contract settles)

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

    HOST = "https://api.elections.kalshi.com"
    PREFIX = "/trade-api/v2"

    def __init__(self, key_id: str, private_key_path: str, host: str | None = None) -> None:
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.host = host or self.HOST

    def _signed_headers(self, method: str, full_path: str) -> dict:
        """Kalshi signs RSA-PSS-SHA256 over: timestamp(ms) + METHOD + path.

        `full_path` includes the /trade-api/v2 prefix and EXCLUDES the query string.
        """
        import base64
        import time

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        with open(self.private_key_path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        msg = (ts + method.upper() + full_path).encode()
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

    def _get(self, endpoint: str, query: str = "") -> dict:
        import httpx

        full_path = self.PREFIX + endpoint              # signed (no query)
        headers = self._signed_headers("GET", full_path)
        resp = httpx.get(self.host + full_path + query, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # Markets closing within this window are the ones actually being traded; a bare
    # ?status=open query returns thousands of dead auto-generated markets first.
    # Lower = shorter-horizon / "daytrade" markets. Env-tunable.
    CLOSE_WINDOW_DAYS = float(os.environ.get("MARKET_HORIZON_DAYS", 7))
    MAX_PAGES = 8

    def list_markets(self) -> list[Market]:
        import time

        now = int(time.time())
        window = now + int(self.CLOSE_WINDOW_DAYS * 86400)
        out: list[Market] = []
        cursor = ""
        for _ in range(self.MAX_PAGES):
            q = f"?status=open&min_close_ts={now}&max_close_ts={window}&limit=1000"
            if cursor:
                q += f"&cursor={cursor}"
            data = self._get("/markets", q)
            for raw in data.get("markets", []):
                # KXMVE* are multi-game parlay legs — near-0 priced, not clean binary events.
                if str(raw.get("ticker", "")).startswith("KXMVE"):
                    continue
                out.append(self._to_market(raw))
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        return out

    def get_market(self, market_id: str) -> Market | None:
        data = self._get(f"/markets/{market_id}")
        m = data.get("market")
        return self._to_market(m) if m else None

    @staticmethod
    def _fnum(x) -> float:
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _to_market(cls, m: dict) -> Market:
        # Kalshi returns prices as `*_dollars` strings already in 0–1 (NOT cents) and
        # sizes as `*_fp` strings. Prefer last trade, then bid/ask mid, then bid.
        bid = cls._fnum(m.get("yes_bid_dollars"))
        ask = cls._fnum(m.get("yes_ask_dollars"))
        last = cls._fnum(m.get("last_price_dollars"))
        yes = last or ((bid + ask) / 2 if ask else bid)
        status = "resolved" if m.get("status") in ("settled", "finalized") else "open"
        outcome = {"yes": 1, "no": 0}.get(m.get("result", ""))
        return Market(
            id=m.get("ticker", "?"),
            title=m.get("title", ""),
            yes_price=min(max(yes, 0.0), 1.0),
            volume=int(cls._fnum(m.get("volume_fp"))),
            status=status,
            outcome=outcome,
            close_ts=cls._parse_ts(m.get("close_time")),
            yes_bid=bid,
            yes_ask=ask,
            rules=(str(m.get("rules_primary", "") or "")
                   + (" " + str(m.get("rules_secondary", "")) if m.get("rules_secondary") else "")).strip()[:600],
        )

    @staticmethod
    def _parse_ts(s) -> int:
        if not s:
            return 0
        try:
            return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            return 0
