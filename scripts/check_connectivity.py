#!/usr/bin/env python3
"""Probe each configured role model via LiteLLM. Live check — needs API keys.

Confirms all three models are reachable and prints latency + cost per call, so
you can validate setup before running a real council task.

    python scripts/check_connectivity.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from council.config import load_settings  # noqa: E402
from council.models import ModelClient, Role  # noqa: E402


def main() -> int:
    settings = load_settings()
    client = ModelClient(budget_usd=settings.budget_usd)
    prompt = [{"role": "user", "content": "Reply with exactly: OK"}]
    failures = 0

    for role in (Role.ARCHITECT, Role.IMPLEMENTER, Role.REVIEWER):
        spec = settings.spec(role)
        print(f"[{role}] {spec.model} … ", end="", flush=True)
        t0 = time.time()
        try:
            text, usage = client.complete(spec, prompt)
            dt = time.time() - t0
            print(f"ok ({dt:.2f}s, {usage.total_tokens} tok, ${usage.cost_usd:.4f}) -> {text.strip()[:40]!r}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL: {type(exc).__name__}: {exc}")

    print(f"\ncumulative: {client.total.total_tokens} tok, ${client.total.cost_usd:.4f}")
    if failures:
        print(f"{failures} model(s) unreachable — check keys/model ids in config.yaml + .env")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
