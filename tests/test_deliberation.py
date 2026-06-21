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

def test_roundtable_one_shared_conversation_kimi_last():
    probs = {_PANEL[0].model: 0.58, _PANEL[1].model: 0.54, _PANEL[2].model: 0.52}
    client = RoundtableClient(probs)
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = DeliberativeCouncil(_PANEL, client).debate(m, "Jobs report hot.")
    assert len(client.messages) == 3            # ONE turn each, not 6 isolated calls
    assert len(d.round2) == 3
    assert d.round2[-1].model == "Kimi K2"      # most-capable model closes the debate
    assert abs(d.converged_p - (0.58+0.54+0.52)/3) < 1e-4
    assert d.spread > 0

def test_later_speakers_see_earlier_turns():
    client = RoundtableClient({s.model: 0.5 for s in _PANEL})
    DeliberativeCouncil(_PANEL, client).debate(Market("X", "x?", 0.5), "notes")
    first_prompt = client.messages[0][-1]["content"]
    last_prompt = client.messages[-1][-1]["content"]
    assert "speak first" in first_prompt.lower()         # opener has no transcript yet
    assert "Claude" in last_prompt and "GPT-5.4" in last_prompt  # Kimi sees both priors

def test_parse_prob_handles_p_yes_line_and_defers():
    from council.trading.deliberation import _parse_prob
    m = Market("X", "x?", 0.37)
    assert abs(_parse_prob("I lean yes. P(YES): 0.82", m) - 0.82) < 1e-9
    assert abs(_parse_prob("call it P(YES): 85%", m) - 0.85) < 1e-9
    assert _parse_prob("no number stated here", m) == 0.37   # defers to market price
