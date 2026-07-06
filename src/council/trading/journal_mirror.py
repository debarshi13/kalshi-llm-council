"""Best-effort mirror of journal trades into the Obsidian vault (browsable record).
Never raises into the trading loop."""
from __future__ import annotations

import pathlib


def mirror_trade(vault_dir: str, row: dict) -> None:
    try:
        d = pathlib.Path(vault_dir) / "wiki" / "trades"
        d.mkdir(parents=True, exist_ok=True)
        mid = row.get("market_id", "unknown")
        outcome = row.get("outcome")
        status = "RESOLVED" if outcome else str(row.get("status", "open")).upper()
        body = (f"---\ntype: trade\nmarket: \"{mid}\"\nstatus: {status.lower()}\n"
                f"side: {row.get('side')}\n---\n\n"
                f"# {row.get('title', '')} ({mid})\n\n"
                f"- Side: **{str(row.get('side', '')).upper()}** x{row.get('contracts')} "
                f"@ {row.get('fill_price')}\n"
                f"- Status: {status}\n"
                f"- Decision: {row.get('decision_reason', '')}\n"
                f"- Council: {row.get('rationale', '')}\n")
        if outcome:
            body += (f"- Outcome: **{str(outcome).upper()}** "
                     f"({'correct' if row.get('council_correct') else 'wrong'}), "
                     f"realized {row.get('realized_pnl')}\n")
        (d / f"{mid}.md").write_text(body)
    except Exception:  # noqa: BLE001 — mirroring must never break trading
        pass


def mirror_calibration(vault_dir: str, report: dict) -> None:
    """Overwrite the single calibration page — the honest scoreboard, in the vault."""
    import datetime
    from pathlib import Path

    page = Path(vault_dir) / "wiki" / "sources" / "Council Calibration Report.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    verdict = ("BEATS the market — live arming unlocked." if report.get("beats_market")
               else "does NOT beat the market — stay on paper.")
    page.write_text(
        f"---\ntype: source\ntitle: \"Council Calibration Report\"\n"
        f"updated: {datetime.date.today().isoformat()}\ntags:\n  - trading\n  - calibration\n---\n\n"
        f"# Council Calibration Report\n\n"
        f"Resolved predictions: **{report['n']}**\n\n"
        f"| Estimator | Brier (lower = better) |\n|---|---|\n"
        f"| Council (converged) | {report.get('brier_model')} |\n"
        f"| Market price baseline | {report.get('brier_market')} |\n"
        f"| Council (blind round 1) | {report.get('brier_blind')} |\n\n"
        f"**Verdict:** the council currently {verdict}\n",
        encoding="utf-8")
