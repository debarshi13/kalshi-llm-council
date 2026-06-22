import time
from council.trading.market import Market


def _m(id="X", price=0.50, volume=1000, close_ts=0, bid=0.0, ask=0.0):
    return Market(id=id, title="t", yes_price=price, volume=volume,
                  close_ts=close_ts, yes_bid=bid, yes_ask=ask)


def test_structural_score_tail_detection():
    """Markets at extreme prices (longshot/near-cert) score higher than mid-priced."""
    from council.trading.scout import structural_score
    mid = structural_score(_m(price=0.50, volume=1000))
    longshot = structural_score(_m(price=0.05, volume=1000))
    near_cert = structural_score(_m(price=0.95, volume=1000))
    assert longshot > mid
    assert near_cert > mid


def test_structural_score_wide_spread():
    """Wide bid/ask spread scores higher than tight spread."""
    from council.trading.scout import structural_score
    tight = structural_score(_m(bid=0.49, ask=0.51))
    wide = structural_score(_m(bid=0.30, ask=0.70))
    assert wide > tight


def test_structural_score_closing_soon():
    """Markets closing in hours score higher than those closing in days."""
    from council.trading.scout import structural_score
    now = int(time.time())
    soon = structural_score(_m(close_ts=now + 3600))       # 1 hour
    later = structural_score(_m(close_ts=now + 86400 * 7)) # 7 days
    assert soon > later


def test_structural_score_stale_price_high_volume():
    """High volume + mid price (not moved) scores higher than low volume."""
    from council.trading.scout import structural_score
    stale_busy = structural_score(_m(price=0.50, volume=50000))
    stale_quiet = structural_score(_m(price=0.50, volume=500))
    assert stale_busy > stale_quiet


def test_shortlist_returns_top_n():
    """shortlist(n=3) on 5 markets returns 3, best structural_score first."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    now = int(time.time())
    markets = [
        _m(id="A", price=0.50, volume=100, close_ts=now + 86400 * 7),
        _m(id="B", price=0.05, volume=1000, close_ts=now + 3600),       # longshot + closing soon
        _m(id="C", price=0.95, volume=2000, close_ts=now + 7200),       # near-cert + high vol
        _m(id="D", price=0.50, volume=200, close_ts=now + 86400),
        _m(id="E", price=0.50, volume=500, close_ts=now + 86400 * 3),
    ]
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=3, max_escalate=1)
    result = scout.shortlist(markets)
    assert len(result) == 3
    ids = [m.id for m in result]
    # B and C should be in top 3 (tail + urgency); A should NOT be (boring mid-price, far out)
    assert "B" in ids and "C" in ids
    assert "A" not in ids


def test_shortlist_empty_pool():
    """shortlist([]) returns []."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=8, max_escalate=1)
    assert scout.shortlist([]) == []


def test_shortlist_fewer_than_n():
    """When pool < shortlist_n, return all markets."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    markets = [_m(id="A"), _m(id="B")]
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.shortlist(markets)
    assert len(result) == 2


from council.models import ModelClient, ModelSpec, Usage
from council.trading.journal import Journal


class _FakeScoutClient(ModelClient):
    """Fake that captures the prompt and returns a canned response."""
    def __init__(self, response: str):
        super().__init__(budget_usd=100.0)
        self.response = response
        self.captured_messages: list[list[dict]] = []

    def _invoke(self, spec, messages):
        self.captured_messages.append(messages)
        return self.response, Usage()


def _journal():
    return Journal(":memory:")


def test_pick_flags_valid_market():
    """pick() returns the matching Market when the scout flags a valid ticker."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": ["MKT-A"]}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    markets = [_m(id="MKT-A"), _m(id="MKT-B")]
    result = scout.pick(markets, _journal())
    assert len(result) == 1
    assert result[0].id == "MKT-A"


def test_pick_returns_empty_on_none():
    """pick() returns [] when the scout says NONE."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": []}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([_m(id="MKT-A")], _journal())
    assert result == []


def test_pick_returns_empty_on_malformed():
    """pick() returns [] (fail closed) on garbage response."""
    from council.trading.scout import Scout
    client = _FakeScoutClient("this is total garbage with no JSON at all")
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([_m(id="MKT-A")], _journal())
    assert result == []


def test_pick_filters_hallucinated_ticker():
    """pick() ignores market IDs not in the input list."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": ["FAKE-TICKER"]}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([_m(id="MKT-A")], _journal())
    assert result == []


def test_pick_caps_at_max_escalate():
    """Even if the scout flags 5 markets, only max_escalate=1 is returned."""
    from council.trading.scout import Scout
    client = _FakeScoutClient('{"escalate": ["A", "B", "C", "D", "E"]}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    markets = [_m(id=x) for x in "ABCDE"]
    result = scout.pick(markets, _journal())
    assert len(result) == 1
    assert result[0].id == "A"


def test_pick_empty_shortlist_no_api_call():
    """pick([]) returns [] without making any API call."""
    from council.trading.scout import Scout
    client = _FakeScoutClient("should not be called")
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    result = scout.pick([], _journal())
    assert result == []
    assert client.captured_messages == []


def test_pick_injects_journal_calibration_and_recall():
    """The prompt sent to the scout model contains journal calibration stats
    and per-market recall lessons."""
    from council.trading.scout import Scout
    j = _journal()
    # Seed the journal with a resolved trade so calibration + recall are non-empty
    j.log(market_id="MKT-A", title="t", side="no", converged_p=0.34, spread=0.02,
          market_price=0.93, executable_price=0.07, edge=0.10, contracts=3,
          fill_price=0.07, fee=0.01, fill_count=3, decision_reason="r", rationale="x")
    j.resolve("MKT-A", "yes")  # NO lost

    client = _FakeScoutClient('{"escalate": []}')
    scout = Scout(client=client, model="test/model", shortlist_n=8, max_escalate=1)
    scout.pick([_m(id="MKT-A")], j)

    # Flatten all message content into one string
    all_text = " ".join(msg["content"] for conv in client.captured_messages for msg in conv)
    # calibration() stats should appear
    assert "overall" in all_text.lower() or "correct" in all_text.lower()
    # recall(market_id) lessons should appear
    assert "MKT" in all_text
    assert "LESSONS" in all_text


def test_mock_scout_shortlist_returns_subset():
    """MockScout.shortlist returns up to shortlist_n markets."""
    from council.trading.scout import MockScout
    markets = [_m(id=x) for x in "ABCDE"]
    mock = MockScout(shortlist_n=3, max_escalate=1)
    result = mock.shortlist(markets)
    assert len(result) <= 3
    assert all(m in markets for m in result)


def test_mock_scout_pick_returns_at_most_max_escalate():
    """MockScout.pick returns 0 or 1 markets (max_escalate=1), no API calls."""
    from council.trading.scout import MockScout
    markets = [_m(id="A"), _m(id="B")]
    mock = MockScout(shortlist_n=8, max_escalate=1)
    # Run multiple times to verify it never exceeds max_escalate
    for _ in range(20):
        result = mock.pick(markets, _journal())
        assert len(result) <= 1
        assert all(m in markets for m in result)


def test_mock_scout_empty_input():
    """MockScout handles empty inputs gracefully."""
    from council.trading.scout import MockScout
    mock = MockScout(shortlist_n=8, max_escalate=1)
    assert mock.shortlist([]) == []
    assert mock.pick([], _journal()) == []


def test_floor_init_has_mock_scout(monkeypatch):
    """FloorState() in default paper mode wires a MockScout."""
    monkeypatch.delenv("SCOUT_MODEL", raising=False)
    from council.trading.floor import FloorState
    from council.trading.scout import MockScout
    f = FloorState()
    assert isinstance(f.scout, MockScout)
    assert f.scout.shortlist_n == 8
    assert f.scout.max_escalate == 1


def test_floor_init_custom_env(monkeypatch):
    """FloorState reads SCOUT_SHORTLIST and SCOUT_MAX_ESCALATE from env."""
    monkeypatch.setenv("SCOUT_SHORTLIST", "4")
    monkeypatch.setenv("SCOUT_MAX_ESCALATE", "2")
    from council.trading.floor import FloorState
    f = FloorState()
    assert f.scout.shortlist_n == 4
    assert f.scout.max_escalate == 2


def test_floor_init_scout_disabled(monkeypatch):
    """SCOUT_MODEL="" disables the scout -- floor.scout is None."""
    monkeypatch.setenv("SCOUT_MODEL", "")
    from council.trading.floor import FloorState
    f = FloorState()
    assert f.scout is None
