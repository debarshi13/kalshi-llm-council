"""Log a completed council run into the Obsidian vault as a wiki source page.

Filesystem transport (matches the vault's detected transport). Best-effort:
the runner swallows failures so logging never breaks a run.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .config import Settings
from .council import RunResult


def _slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:limit] or "run"


def log_run(settings: Settings, result: RunResult, task: str) -> Path:
    if not settings.obsidian.enabled or not settings.obsidian.vault:
        raise RuntimeError("obsidian logging is disabled or vault unset")
    vault = Path(settings.obsidian.vault)
    if not vault.is_dir():
        raise FileNotFoundError(f"vault not found: {vault}")

    out_dir = vault / settings.obsidian.runs_subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    page = out_dir / f"Council Run {result.run_id}.md"

    u = result.usage
    content = f"""---
type: source
title: "Council Run {result.run_id}"
created: {today}
updated: {today}
tags:
  - council-run
  - ai-agents
status: seed
source_type: data
confidence: high
related:
  - "[[Multi-Model AI Council]]"
  - "[[Multi-Model Council Architecture Decision]]"
---

# Council Run {result.run_id}

- **Task:** {task}
- **Status:** `{result.status.value}`
- **Revise rounds:** {result.rounds}
- **Tokens:** {u.total_tokens} ({u.prompt_tokens} in / {u.completion_tokens} out)
- **Cost:** ${u.cost_usd:.4f}
- **Workspace:** `{result.workspace}`
- **Artifacts:** `{result.artifacts_dir}`

## Outcome
{result.final_output}

> [!note] Auto-logged by the [[Multi-Model AI Council]] mission control.
"""
    page.write_text(content)
    return page
