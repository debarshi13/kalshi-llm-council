from council.trading.deliberation import Deliberation, ModelEstimate, Decision, decide, DeliberativeCouncil
from council.trading.execution import RiskGuard
from council.trading.market import Market
from council.models import ModelSpec

CAPS = RiskGuard(max_position_usd=5, max_total_exposure_usd=50, max_daily_loss_usd=20)

def _delib(market_id, converged_p, spread):
    return Deliberation(market_id, [], [], converged_p, spread, "")


class RecordingClient:
    """Returns scripted JSON per model; records messages so we can assert round-2 context."""
    def __init__(self, r1: dict[str, float], r2: dict[str, float]):
        self.r1, self.r2 = r1, r2
        self.messages = []
    def complete(self, spec, messages):
        self.messages.append(messages)
        user = messages[-1]["content"]
        table = self.r2 if "PEER ESTIMATES" in user else self.r1
        p = table[spec.model]
        return f'{{"prob_yes": {p}, "thesis": "model {spec.model} says {p}"}}', {}

def _council():
    specs = [ModelSpec("m-claude", "g"), ModelSpec("m-kimi", "g"), ModelSpec("m-glm", "g")]
    return specs

def test_consensus_place_no_side():
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = _delib(m.id, 0.533, 0.018)          # mean below price -> NO; tight spread
    out = decide(d, m, CAPS)
    assert out.place is True
    assert out.side == "no"
    # NO entry = 1 - 0.62 = 0.38 -> 38c; contracts = floor(5/0.38)=13
    assert out.limit_price_cents == 38
    assert out.contracts == 13

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

def test_debate_runs_two_rounds_and_converges():
    specs = _council()
    client = RecordingClient(
        r1={"m-claude": 0.58, "m-kimi": 0.49, "m-glm": 0.55},
        r2={"m-claude": 0.54, "m-kimi": 0.52, "m-glm": 0.54},
    )
    m = Market("FED-DEC-CUT", "Fed cuts?", 0.62)
    d = DeliberativeCouncil(specs, client).debate(m, "Jobs report hot.")
    assert [e.model for e in d.round1] == ["m-claude", "m-kimi", "m-glm"]
    assert len(d.round2) == 3
    assert abs(d.converged_p - (0.54+0.52+0.54)/3) < 1e-4
    assert d.spread > 0
    # 6 model calls total (3 per round)
    assert len(client.messages) == 6

def test_round2_prompt_includes_peer_estimates():
    specs = _council()
    client = RecordingClient(r1={"m-claude":0.5,"m-kimi":0.5,"m-glm":0.5},
                             r2={"m-claude":0.5,"m-kimi":0.5,"m-glm":0.5})
    m = Market("X", "x?", 0.5)
    DeliberativeCouncil(specs, client).debate(m, "notes")
    round2_msgs = [msg for msg in client.messages if "PEER ESTIMATES" in msg[-1]["content"]]
    assert len(round2_msgs) == 3
    # a peer's round-1 number appears in the round-2 prompt
    assert "0.5" in round2_msgs[0][-1]["content"]

def test_unparseable_reply_defers_to_market_price():
    specs = [ModelSpec("m1", "g")]
    class Junk:
        def complete(self, spec, messages): return "no json here", {}
    m = Market("X", "x?", 0.37)
    d = DeliberativeCouncil(specs, Junk()).debate(m, "")
    assert d.round1[0].p_yes == 0.37   # parse_estimate defers to market price
