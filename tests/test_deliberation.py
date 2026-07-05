from council.trading.deliberation import Deliberation, ModelEstimate, Decision, decide, DeliberativeCouncil
from council.trading.execution import RiskGuard
from council.trading.market import Market
from council.models import ModelSpec

CAPS = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)

def _delib(market_id, converged_p, spread):
    return Deliberation(market_id, [], [], converged_p, spread, "")


class RoundtableClient:
    """Returns a roundtable message ending in a 'P(YES): x' line per model; records the
    prompts each speaker received so we can assert the conversation is shared."""
    def __init__(self, probs: dict[str, float]):
        self.probs = probs
        self.messages = []
    def complete(self, spec, messages):
        self.messages.append(messages)
        return f"My read on it. P(YES): {self.probs[spec.model]}", {}

_PANEL = [ModelSpec("openrouter/anthropic/claude-opus-4.8", "g"),
          ModelSpec("openrouter/openai/gpt-5.4", "g"),
          ModelSpec("openrouter/moonshotai/kimi-k2.6", "g")]   # Kimi speaks last

def test_consensus_place_no_side():
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = _delib(m.id, 0.533, 0.018)          # mean below price -> NO; tight spread
    out = decide(d, m, CAPS)
    assert out.place is True
    assert out.side == "no"
    # NO entry = 1 - 0.62 = 0.38 -> 38c; conviction-scaled size (modest edge) -> 5 contracts
    assert out.limit_price_cents == 38
    assert out.contracts == 5

def test_conviction_sizing_scales_with_edge():
    m = Market("X", "x?", 0.50)
    small = decide(_delib(m.id, 0.58, 0.0), m, CAPS)   # 8c edge, full agreement
    big = decide(_delib(m.id, 0.72, 0.0), m, CAPS)     # 22c edge -> full conviction
    assert small.place and big.place
    assert big.contracts > small.contracts             # bigger edge -> bigger bet (within cap)

def test_consensus_place_yes_side():
    m = Market("CPI-NOV-HOT", "CPI hot?", 0.41)
    d = _delib(m.id, 0.52, 0.02)            # mean above price -> YES, 11c edge
    out = decide(d, m, CAPS)
    assert out.place is True and out.side == "yes"
    assert out.limit_price_cents == 41

def test_high_spread_skips():
    m = Market("X", "x?", 0.50)
    d = _delib(m.id, 0.70, 0.12)            # big edge but no consensus
    out = decide(d, m, CAPS)
    assert out.place is False and "consensus" in out.reason.lower()

def test_subthreshold_edge_skips():
    m = Market("X", "x?", 0.50)
    d = _delib(m.id, 0.53, 0.01)            # 3c edge < 6c
    out = decide(d, m, CAPS)
    assert out.place is False and "threshold" in out.reason.lower()

def test_price_clamped_to_valid_range():
    m = Market("X", "x?", 0.95)
    d = _delib(m.id, 0.99, 0.0)             # YES, entry 0.95 -> 95c valid
    out = decide(d, m, CAPS)
    assert 1 <= out.limit_price_cents <= 99

def test_roundtable_two_rounds_kimi_last():
    probs = {_PANEL[0].model: 0.58, _PANEL[1].model: 0.54, _PANEL[2].model: 0.52}
    client = RoundtableClient(probs)
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = DeliberativeCouncil(_PANEL, client, alpha=1.0).debate(m, "Jobs report hot.")
    assert len(client.messages) == 6            # 3 blind turns + 3 reveal turns
    assert len(d.round1) == 3 and len(d.round2) == 3
    assert d.round2[-1].model == "Kimi K2"      # most-capable model closes the debate
    assert d.converged_p == statistics.median([0.58, 0.54, 0.52])   # alpha=1.0 -> no extremization
    assert d.spread > 0

def test_round2_reveals_all_round1_blind_estimates_to_every_speaker():
    client = RoundtableClient({s.model: 0.5 for s in _PANEL})
    DeliberativeCouncil(_PANEL, client).debate(Market("X", "x?", 0.5), "notes")
    round1_sys = [msgs[0]["content"] for msgs in client.messages[:3]]
    round2_sys = [msgs[0]["content"] for msgs in client.messages[3:]]
    for sys_msg in round1_sys:
        assert "Blind estimates" not in sys_msg          # round 1 is isolated
    for sys_msg in round2_sys:
        # every round-2 speaker (not just the last) sees all three blind estimates
        assert "Claude" in sys_msg and "GPT-5.4" in sys_msg and "Kimi K2" in sys_msg

def test_parse_prob_handles_p_yes_line_and_defers():
    from council.trading.deliberation import _parse_prob
    m = Market("X", "x?", 0.37)
    assert abs(_parse_prob("I lean yes. P(YES): 0.82", m) - 0.82) < 1e-9
    assert abs(_parse_prob("call it P(YES): 85%", m) - 0.85) < 1e-9
    assert _parse_prob("no number stated here", m) == 0.37   # defers to market price


def test_decide_wide_spread_erases_edge_skips():
    # mid looks like a 5c YES edge, but the ask is 0.70 -> no real edge -> SKIP (the T30 fix)
    m = Market("X", "x?", 0.50, yes_bid=0.30, yes_ask=0.70)
    out = decide(_delib(m.id, 0.55, 0.0), m, CAPS)
    assert out.place is False

def test_decide_tight_spread_places_executable():
    m = Market("X", "x?", 0.50, yes_bid=0.49, yes_ask=0.51)
    out = decide(_delib(m.id, 0.62, 0.0), m, CAPS)   # 0.62 - 0.51 ask = 11c YES edge
    assert out.place is True and out.side == "yes"

def test_decide_falls_back_to_mid_without_quote():
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)     # no bid/ask -> mid fallback
    out = decide(_delib(m.id, 0.533, 0.018), m, CAPS)
    assert out.place is True and out.side == "no" and out.limit_price_cents == 38

def test_debate_prompt_has_rules_in_both_rounds_but_quote_only_in_round2():
    client = RoundtableClient({s.model: 0.5 for s in _PANEL})
    m = Market("X", "x?", 0.50, yes_bid=0.48, yes_ask=0.52, rules="Resolves YES if above 60.")
    DeliberativeCouncil(_PANEL, client).debate(m, "some notes")
    round1_sys = [msgs[0]["content"] for msgs in client.messages[:3]]
    round2_sys = [msgs[0]["content"] for msgs in client.messages[3:]]
    for sys_msg in round1_sys:
        assert "above 60" in sys_msg
        assert "0.48" not in sys_msg and "0.52" not in sys_msg     # blind: no quote
        assert "independent view" in sys_msg.lower()
    for sys_msg in round2_sys:
        assert "above 60" in sys_msg
        assert "0.48" in sys_msg and "0.52" in sys_msg             # revealed
        assert "do not reflexively defer" in sys_msg.lower()

def test_debate_prompt_includes_lessons():
    client = RoundtableClient({s.model: 0.5 for s in _PANEL})
    m = Market("X", "x?", 0.5)
    DeliberativeCouncil(_PANEL, client).debate(m, "notes", lessons="LESSONS: your >25c edges 1/8.")
    allmsgs = " ".join(msg["content"] for conv in client.messages for msg in conv)
    assert "your >25c edges 1/8" in allmsgs


import statistics
from council.trading.deliberation import DeliberativeCouncil
from council.trading.market import Market
from council.models import ModelSpec


class ScriptedClient:
    """Returns scripted texts in order; records every prompt it saw."""
    def __init__(self, texts):
        self.texts = list(texts)
        self.prompts = []

    def complete(self, spec, messages):
        self.prompts.append((messages[0]["content"], messages[1]["content"]))
        return self.texts.pop(0), None


def _mkt():
    return Market("KXQ-1", "Will it rain?", 0.80, volume=500,
                  yes_bid=0.78, yes_ask=0.82, rules="Resolves YES if NWS reports rain.")


def test_round1_is_blind_no_price_no_peers():
    texts = ["A. P(YES): 0.60", "B. P(YES): 0.70", "C. P(YES): 0.65",
             "A2. P(YES): 0.62", "B2. P(YES): 0.70", "C2. P(YES): 0.66"]
    client = ScriptedClient(texts)
    council = DeliberativeCouncil([ModelSpec("openrouter/anthropic/claude-opus-4.8", "x"),
                                   ModelSpec("openrouter/moonshotai/kimi-k2.6", "x"),
                                   ModelSpec("openrouter/openai/gpt-5.4", "x")], client)
    d = council.debate(_mkt(), "some research")
    # first 3 calls are round 1: no market price anywhere in the prompt
    for sys_msg, user_msg in client.prompts[:3]:
        assert "0.80" not in sys_msg and "0.80" not in user_msg
        assert "P(YES): 0.6" not in sys_msg          # no peer estimates leaked
    # last 3 calls are round 2: price and peers revealed
    for sys_msg, user_msg in client.prompts[3:]:
        assert "0.80" in sys_msg or "0.80" in user_msg
    assert len(d.round1) == 3 and len(d.round2) == 3


def test_converged_is_extremized_median_of_round2():
    texts = ["P(YES): 0.60", "P(YES): 0.70", "P(YES): 0.65",
             "P(YES): 0.10", "P(YES): 0.70", "P(YES): 0.72"]   # 0.10 outlier
    client = ScriptedClient(texts)
    council = DeliberativeCouncil([ModelSpec("m/a", "x"), ModelSpec("m/b", "x"),
                                   ModelSpec("m/c", "x")], client, alpha=1.0)
    d = council.debate(_mkt(), "notes")
    assert d.converged_p == 0.70          # median ignores the outlier; alpha=1 = no shift


def test_mock_council_populates_round1():
    from council.trading.deliberation import MockCouncil
    d = MockCouncil(["A", "B", "C"]).debate(_mkt(), "notes")
    assert len(d.round1) == 3 and len(d.round2) == 3
