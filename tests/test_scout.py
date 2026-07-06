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


def test_structural_score_maker_viable_spread_beats_tight_or_wide():
    """A spread inside the maker-viable band [0.02, 0.15] scores higher than a
    spread too tight to post inside or too wide for a realistic resting fill."""
    from council.trading.scout import structural_score
    viable = structural_score(_m(bid=0.45, ask=0.55))    # 0.10 spread — viable
    tight = structural_score(_m(bid=0.499, ask=0.501))   # 0.002 spread — no room to post
    wide = structural_score(_m(bid=0.20, ask=0.80))      # 0.60 spread — unrealistic fill
    assert viable > tight
    assert viable > wide


def test_structural_score_subhour_excluded():
    """Sub-hour lotteries (e.g. hourly BTC strikes) are untradeable (-inf); markets
    within the horizon are not."""
    from council.trading.scout import structural_score
    now = int(time.time())
    lottery = structural_score(_m(close_ts=now + 1800))       # 30 min
    normal = structural_score(_m(close_ts=now + 86400 * 2))   # 2 days
    assert lottery == float("-inf")
    assert normal > float("-inf")


def test_structural_score_prefers_weak_prices():
    """Tail + thin volume + maker-viable spread outranks mid-price + heavy volume."""
    from council.trading.scout import structural_score
    weak = _m(id="KXA-1", price=0.90, volume=500, close_ts=int(time.time() + 48 * 3600),
              bid=0.87, ask=0.93)
    strong = _m(id="KXB-1", price=0.50, volume=100_000, close_ts=int(time.time() + 48 * 3600),
                bid=0.49, ask=0.51)
    assert structural_score(weak) > structural_score(strong)


def test_shortlist_returns_top_n():
    """shortlist(n=3) on 5 markets returns 3, best edge_score (weak-price) first."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    now = int(time.time())
    markets = [
        _m(id="A", price=0.50, volume=100_000, close_ts=now + 86400 * 3),                 # boring + heavy volume
        _m(id="B", price=0.90, volume=500, close_ts=now + 86400 * 2, bid=0.87, ask=0.93),  # weak + thin + viable spread
        _m(id="C", price=0.10, volume=800, close_ts=now + 86400 * 4, bid=0.07, ask=0.13),  # weak + thin + viable spread
        _m(id="D", price=0.50, volume=200, close_ts=now + 86400 * 5),                     # boring, low-ish volume
        _m(id="E", price=0.50, volume=50_000, close_ts=now + 86400 * 1),                  # boring + heavy volume
    ]
    scout = Scout(client=ModelClient(), model="test/model", shortlist_n=3, max_escalate=1)
    result = scout.shortlist(markets)
    assert len(result) == 3
    ids = [m.id for m in result]
    # B and C (weak-priced, thin, maker-viable spread) should be in top 3; A (boring, heavy volume) should not
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


from council.trading.deliberation import Deliberation, ModelEstimate
from council.trading.floor import FloorState
from council.trading.execution import RiskGuard


class _FixedScout:
    """Test scout that returns a fixed result."""
    def __init__(self, escalate_ids: list[str], shortlist_n=8, max_escalate=1):
        self._escalate_ids = set(escalate_ids)
        self.shortlist_n = shortlist_n
        self.max_escalate = max_escalate
        self.shortlist_called = False
        self.pick_called = False

    def shortlist(self, markets):
        self.shortlist_called = True
        return markets[:self.shortlist_n]

    def pick(self, markets, journal):
        self.pick_called = True
        return [m for m in markets if m.id in self._escalate_ids][:self.max_escalate]


class _FakeCouncilForScout:
    specs = [1, 2, 3]
    def __init__(self):
        self.debated = []
    def debate(self, market, notes, lessons=""):
        self.debated.append(market.id)
        e = [ModelEstimate("a", 0.50, "t"), ModelEstimate("b", 0.50, "t")]
        return Deliberation(market.id, e, e, 0.50, 0.01, notes)

class _FakeResearchForScout:
    def context_for(self, market): return "notes"


def _floor_with_scout(scout, council=None, live=False):
    f = FloorState()
    f.scout = scout
    f.council = council or _FakeCouncilForScout()
    f.research = _FakeResearchForScout()
    f.live = live
    if live:
        f._live_markets = [
            _m(id="MKT-A", price=0.62, volume=1000),
            _m(id="MKT-B", price=0.50, volume=2000),
        ]
        f.execute = False  # live but unarmed
    else:
        f.markets = [
            _m(id="MKT-A", price=0.62, volume=1000),
            _m(id="MKT-B", price=0.50, volume=2000),
        ]
    f.guard = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)
    return f


def test_funnel_escalated_debates_flagged_market():
    """When the scout flags MKT-A, council.debate() is called exactly once on MKT-A."""
    council = _FakeCouncilForScout()
    f = _floor_with_scout(_FixedScout(escalate_ids=["MKT-A"]), council=council)
    f._council_eval()
    assert council.debated == ["MKT-A"]


def test_funnel_nothing_escalated_no_debate():
    """When the scout returns [], council.debate() is never called."""
    council = _FakeCouncilForScout()
    f = _floor_with_scout(_FixedScout(escalate_ids=[]), council=council)
    f._council_eval()
    assert council.debated == []
    assert any("no candidates escalated" in a["s"].lower() for a in f.activity)


def test_funnel_call_counter_increments_by_one():
    """In live mode, the scout call increments self.calls by 1."""
    f = _floor_with_scout(_FixedScout(escalate_ids=["MKT-A"]), live=True)
    initial_calls = f.calls
    f._council_eval()
    # scout call = +1, debate call = +1 research + 2 rounds x len(specs); total = +1 + 1 + 2*len(specs)
    assert f.calls == initial_calls + 1 + 1 + 2 * len(f.council.specs)


def test_funnel_backward_compat_scout_disabled(monkeypatch):
    """With scout=None (SCOUT_MODEL=""), the old cheap_score path fires."""
    monkeypatch.setenv("SCOUT_MODEL", "")
    f = FloorState()
    council = _FakeCouncilForScout()
    f.council = council
    f.research = _FakeResearchForScout()
    assert f.scout is None
    f._council_eval()
    # Old path: picks max by cheap_score and debates it
    assert len(council.debated) == 1


# ── series-diversity cap (stop one busy series monopolizing the shortlist) ──
def test_shortlist_excludes_untradeable_markets():
    """_diverse_shortlist filters out markets scoring -inf (untradeable: below min volume,
    sub-hour, or beyond horizon)."""
    import math
    from council.trading.scout import _diverse_shortlist, structural_score
    now = int(time.time())
    tradeable = _m(id="KXFED-1", price=0.90, volume=500, close_ts=now + 48 * 3600,
                   bid=0.87, ask=0.93)
    subhour = _m(id="KXBTCD-1", price=0.50, volume=500, close_ts=now + 1800)
    thin = _m(id="KXCPI-1", price=0.50, volume=5, close_ts=now + 48 * 3600)
    assert structural_score(subhour) == -math.inf and structural_score(thin) == -math.inf
    out = _diverse_shortlist([tradeable, subhour, thin], n=8, max_per_series=2)
    assert out == [tradeable]


def test_shortlist_caps_per_series():
    """No more than max_per_series markets from one series make the shortlist, so
    hourly BTC strikes can't crowd out everything else."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    now = int(time.time())
    # 5 high-scoring BTC strikes (same series) + 2 lower-scoring other-series markets
    # Use now + 48*3600 for tradeable horizon (instead of now + 3600 which is sub-hour)
    markets = [_m(id=f"KXBTCD-{i}", price=0.50, volume=50000, close_ts=now + 48 * 3600) for i in range(5)]
    markets += [_m(id="KXFED-1", price=0.50, volume=1000, close_ts=now + 48 * 3600),
                _m(id="KXCPI-1", price=0.50, volume=1000, close_ts=now + 48 * 3600)]
    scout = Scout(client=ModelClient(), model="m", shortlist_n=5, max_escalate=1, max_per_series=2)
    result = scout.shortlist(markets)
    btc = [m for m in result if m.id.startswith("KXBTCD")]
    assert len(btc) <= 2                                   # BTC capped
    assert any(not m.id.startswith("KXBTCD") for m in result)  # other series get a look


def test_shortlist_diversity_preserves_ranking_when_no_collision():
    """With all-distinct series, the cap is inert — ranking is unchanged."""
    from council.trading.scout import Scout
    from council.models import ModelClient
    now = int(time.time())
    markets = [_m(id="A-1", price=0.50, volume=100, close_ts=now + 86400 * 7),
               _m(id="B-1", price=0.05, volume=1000, close_ts=now + 86400 * 2),
               _m(id="C-1", price=0.95, volume=2000, close_ts=now + 7200)]
    scout = Scout(client=ModelClient(), model="m", shortlist_n=2, max_escalate=1, max_per_series=2)
    ids = [m.id for m in scout.shortlist(markets)]
    assert "B-1" in ids and "C-1" in ids and "A-1" not in ids


# ── re-debate cooldown (sustain the rate; let prices move before revisiting) ──
def test_cooldown_excludes_recently_debated():
    """A market debated within the cooldown is not re-debated next cycle."""
    council = _FakeCouncilForScout()
    f = _floor_with_scout(_FixedScout(escalate_ids=["MKT-A"]), council=council)
    f.COUNCIL_COOLDOWN_SEC = 1800
    f._council_eval()
    assert council.debated == ["MKT-A"]
    f._council_eval()                       # MKT-A on cooldown; MKT-B not flagged
    assert council.debated == ["MKT-A"]     # no second debate


def test_cooldown_allows_redebate_after_expiry():
    """Once the cooldown has elapsed, a market is eligible to be debated again."""
    council = _FakeCouncilForScout()
    f = _floor_with_scout(_FixedScout(escalate_ids=["MKT-A"]), council=council)
    f.COUNCIL_COOLDOWN_SEC = 1000
    f._council_eval()
    assert council.debated == ["MKT-A"]
    f._council_seen_ts["MKT-A"] -= 2000     # pretend the cooldown elapsed
    f._council_eval()
    assert council.debated == ["MKT-A", "MKT-A"]
