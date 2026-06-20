from council.trading.research import WebSearchResearch
from council.trading.market import Market

def test_returns_search_notes():
    r = WebSearchResearch(lambda q: f"- 2026-06-19: news about {q}")
    assert "news about Fed cuts?" in r.context_for(Market("FED", "Fed cuts?", 0.6))

def test_blank_search_falls_back():
    r = WebSearchResearch(lambda q: "   ")
    assert r.context_for(Market("X", "x?", 0.5)) == "No external signal available."

def test_search_error_falls_back_gracefully():
    def boom(q): raise RuntimeError("network down")
    r = WebSearchResearch(boom)
    assert r.context_for(Market("X", "x?", 0.5)) == "No external signal available."
