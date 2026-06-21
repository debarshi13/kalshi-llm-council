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
