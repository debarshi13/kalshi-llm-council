import pytest

from council.models import ModelClient, ModelSpec, Usage
from council.trading.analysts import LiteLLMAnalyst, MockResearch, parse_estimate
from council.trading.desk import build_litellm_desk, build_mock_desk
from council.trading.market import Market, MockMarketData


class FakeClient(ModelClient):
    """A ModelClient whose API call is replaced by a canned reply (no network)."""

    def __init__(self, reply: str) -> None:
        super().__init__(budget_usd=10.0)
        self.reply = reply

    def _invoke(self, spec, messages):
        return self.reply, Usage(5, 5, 0.0)


M = Market("FED-DEC-CUT", "Fed cuts in December?", 0.62)


# --- parsing ---------------------------------------------------------------
def test_parse_json_estimate():
    e = parse_estimate('{"prob_yes": 0.85, "thesis": "futures imply higher"}', M)
    assert e.prob_yes == 0.85 and "futures" in e.thesis


def test_parse_loose_probability():
    e = parse_estimate("My read: prob_yes = 0.70 given the polls.", M)
    assert e.prob_yes == 0.70


def test_parse_defers_to_market_when_unparseable():
    e = parse_estimate("I really can't say.", M)
    assert e.prob_yes == M.yes_price  # no invented edge


def test_parse_clamps():
    assert parse_estimate('{"prob_yes": 1.8, "thesis": "x"}', M).prob_yes == 1.0


# --- live analyst (offline via FakeClient) ---------------------------------
def test_litellm_analyst_estimates():
    analyst = LiteLLMAnalyst(
        ModelSpec("mock/analyst", "estimate"),
        "news",
        FakeClient('{"prob_yes": 0.85, "thesis": "stale on fresh data"}'),
        MockResearch({"FED-DEC-CUT": "Dovish Fed minutes released today."}),
    )
    est = analyst.estimate(M)
    assert est.prob_yes == 0.85


# --- desk ------------------------------------------------------------------
def test_mock_desk_runs_three_books():
    md = MockMarketData()
    desk = build_mock_desk(md, beliefs={"B": {"FED-DEC-CUT": 0.85}}, watchlist=["FED-DEC-CUT"])
    res = desk.run_tick()
    assert res["B"] and res["B"][0].side == "yes"   # B has an edge belief
    assert res["A"] == [] and res["C"] == []         # default analyst sees market price = no edge
    assert len(desk.leaderboard()) == 3


def test_litellm_desk_builds_three_books():
    md = MockMarketData()
    desk = build_litellm_desk(md, client=FakeClient("{}"), spec=ModelSpec("mock/x", "g"))
    assert [b.name for b in desk.books] == ["A", "B", "C"]
