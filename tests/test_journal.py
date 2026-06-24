from council.trading.journal import Journal


def _j():
    return Journal(":memory:", clock=lambda: 1000.0)


def _log(j, **kw):
    base = dict(market_id="KXHORMUZWEEKLY-26JUN21-T50", title="t", side="no",
                converged_p=0.34, spread=0.02, market_price=0.93, executable_price=0.07,
                edge=0.10, contracts=3, fill_price=0.07, fee=0.01, fill_count=3,
                decision_reason="r", rationale="debate")
    base.update(kw)
    return j.log(**base)


def test_log_creates_open_row():
    j = _j(); tid = _log(j)
    row = j.get(tid)
    assert row["status"] == "placed" and row["series"] == "KXHORMUZWEEKLY" and row["side"] == "no"
    assert row["outcome"] is None and row["realized_pnl"] is None


def test_mark_updates_unrealized_no_side():
    j = _j(); tid = _log(j, side="no", fill_price=0.07, contracts=3)
    j.mark("KXHORMUZWEEKLY-26JUN21-T50", yes_price=0.80)   # NO now worth 0.20
    row = j.get(tid)
    assert abs(row["unrealized_pnl"] - 3 * (0.20 - 0.07)) < 1e-9


def test_resolve_no_win_and_yes_win():
    j = _j()
    no_t = _log(j, side="no", contracts=3, fill_price=0.07, fee=0.01)
    j.resolve("KXHORMUZWEEKLY-26JUN21-T50", outcome="no")     # NO won
    r = j.get(no_t)
    assert r["status"] == "resolved" and r["council_correct"] == 1
    assert abs(r["realized_pnl"] - (3 * (1.0 - 0.07) - 0.01)) < 1e-9

    yes_t = _log(j, market_id="KXFOO-1", side="yes", contracts=2, fill_price=0.60, fee=0.01)
    j.resolve("KXFOO-1", outcome="no")                        # YES lost
    r2 = j.get(yes_t)
    assert r2["council_correct"] == 0
    assert abs(r2["realized_pnl"] - (2 * (0.0 - 0.60) - 0.01)) < 1e-9


def test_calibration_buckets():
    j = _j()
    _log(j, market_id="KXA-1", edge=0.03, side="yes", fill_price=0.5, contracts=1, fee=0); j.resolve("KXA-1", "yes")
    _log(j, market_id="KXB-1", edge=0.04, side="yes", fill_price=0.5, contracts=1, fee=0); j.resolve("KXB-1", "no")
    _log(j, market_id="KXC-1", edge=0.30, side="no", fill_price=0.1, contracts=1, fee=0); j.resolve("KXC-1", "yes")
    cal = j.calibration()
    assert cal["overall"]["n"] == 3 and cal["overall"]["win"] == 1
    assert cal["by_edge"]["<5c"]["n"] == 2 and cal["by_edge"]["<5c"]["win"] == 1
    assert cal["by_edge"][">25c"]["n"] == 1 and cal["by_edge"][">25c"]["win"] == 0


def test_recall_similar_and_empty():
    j = _j()
    assert "no resolved history" in j.recall("KXHORMUZWEEKLY-26JUN21-T50").lower()
    _log(j, market_id="KXHORMUZWEEKLY-26JUN21-T30", side="no", contracts=1, fill_price=0.9, fee=0)
    j.resolve("KXHORMUZWEEKLY-26JUN21-T30", "yes")   # NO lost
    text = j.recall("KXHORMUZWEEKLY-26JUN21-T99")
    assert "KXHORMUZWEEKLY" in text and "0W/1L" in text


def test_mirror_writes_note(tmp_path):
    from council.trading.journal_mirror import mirror_trade
    row = {"market_id": "KXFOO-1", "title": "Foo?", "side": "no", "contracts": 3,
           "fill_price": 0.07, "status": "placed", "decision_reason": "r",
           "rationale": "Claude 0.34 | Kimi 0.33", "outcome": None,
           "realized_pnl": None, "council_correct": None}
    mirror_trade(str(tmp_path), row)
    note = tmp_path / "wiki" / "trades" / "KXFOO-1.md"
    assert note.exists() and "Foo?" in note.read_text() and "NO" in note.read_text().upper()


def test_mirror_failure_is_silent():
    from council.trading.journal_mirror import mirror_trade
    mirror_trade("/nonexistent/\0bad", {"market_id": "X"})  # must not raise


def test_realized_today():
    import time as _t
    j = Journal(":memory:", clock=_t.time)
    _log(j, market_id="KXZ-1", side="no", contracts=10, fill_price=0.9, fee=0)
    assert j.realized_today() == 0.0              # unresolved
    j.resolve("KXZ-1", "yes")                     # NO lost: 10*(0-0.9) = -9
    assert abs(j.realized_today() - (-9.0)) < 1e-9


def test_open_positions_lists_only_placed():
    j = Journal(":memory:", clock=lambda: 1000.0)
    j.log(market_id="A-1", title="t", side="yes", converged_p=0.6, spread=0.02,
          market_price=0.5, executable_price=0.52, edge=0.08, contracts=10,
          fill_price=0.52, fee=0.01, fill_count=10, decision_reason="r",
          rationale="x", status="placed")
    j.log(market_id="B-2", title="t", side="no", converged_p=0.3, spread=0.02,
          market_price=0.4, executable_price=0.41, edge=0.05, contracts=5,
          fill_price=0.59, fee=0.01, fill_count=5, decision_reason="r",
          rationale="x", status="skipped")
    rows = j.open_positions()
    assert [r["market_id"] for r in rows] == ["A-1"]
    assert rows[0]["fill_price"] == 0.52 and rows[0]["converged_p"] == 0.6

def test_record_exit_closes_row_and_realizes_pnl():
    j = Journal(":memory:", clock=lambda: 1000.0)
    tid = j.log(market_id="A-1", title="t", side="yes", converged_p=0.6, spread=0.02,
                market_price=0.5, executable_price=0.52, edge=0.08, contracts=10,
                fill_price=0.50, fee=0.02, fill_count=10, decision_reason="r",
                rationale="x", status="placed")
    realized = j.record_exit(tid, exit_price=0.58, exit_fee=0.02, fill_count=10)
    # 10 * (0.58 - 0.50) - 0.02 entry - 0.02 exit = 0.80 - 0.04 = 0.76
    assert round(realized, 4) == 0.76
    row = j.get(tid)
    assert row["status"] == "exited" and round(row["realized_pnl"], 4) == 0.76
    assert j.open_positions() == []   # no longer open

def test_record_exit_unknown_id_returns_none():
    j = Journal(":memory:", clock=lambda: 1000.0)
    assert j.record_exit(999, 0.5, 0.0, 1) is None

def test_record_exit_is_idempotent_on_already_exited():
    j = Journal(":memory:", clock=lambda: 1000.0)
    tid = j.log(market_id="A-1", title="t", side="yes", converged_p=0.6, spread=0.02,
                market_price=0.5, executable_price=0.52, edge=0.08, contracts=10,
                fill_price=0.50, fee=0.02, fill_count=10, decision_reason="r",
                rationale="x", status="placed")
    assert j.record_exit(tid, 0.58, 0.02, 10) is not None     # first close works
    assert j.record_exit(tid, 0.40, 0.02, 10) is None         # second is a no-op
    assert round(j.get(tid)["realized_pnl"], 4) == 0.76       # P&L not overwritten

def test_realized_today_includes_exited():
    import time as _t
    j = Journal(":memory:", clock=_t.time)
    tid = j.log(market_id="A-1", title="t", side="yes", converged_p=0.6, spread=0.02,
                market_price=0.5, executable_price=0.52, edge=0.08, contracts=10,
                fill_price=0.50, fee=0.0, fill_count=10, decision_reason="r",
                rationale="x", status="placed")
    j.record_exit(tid, 0.40, 0.0, 10)                          # realized -1.00 (a loss)
    assert round(j.realized_today(), 2) == -1.00               # kill cap must see it
