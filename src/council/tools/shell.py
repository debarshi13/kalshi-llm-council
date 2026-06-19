"""Sandboxed command/test runner.

Runs inside the workspace dir with a hard timeout and capped output. This is a
subprocess sandbox, not a container — a denylist blocks the most catastrophic
patterns, and execution is approval-gated at the council layer. Docker-backed
isolation is the planned hardening step (no Docker on this host yet).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Patterns we refuse to run even with approval — defense in depth.
_DENY = [
    re.compile(r"\brm\s+-rf\s+/(?:\s|$)"),
    re.compile(r"\brm\s+-rf\s+~"),
    re.compile(r":\(\)\s*\{.*\};:"),       # fork bomb
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
    re.compile(r">/dev/sd[a-z]"),
    re.compile(r"\bshutdown\b|\breboot\b"),
]

_MAX_OUTPUT = 20_000  # chars per stream


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "ok": self.ok,
        }


class DeniedCommand(ValueError):
    """Raised when a command matches the destructive-pattern denylist."""


def _cap(text: str) -> str:
    if len(text) > _MAX_OUTPUT:
        return text[:_MAX_OUTPUT] + f"\n...[truncated, {len(text) - _MAX_OUTPUT} more chars]"
    return text


def run_command(command: str, workspace: Path, timeout: int = 120) -> CommandResult:
    for pat in _DENY:
        if pat.search(command):
            raise DeniedCommand(f"refused destructive command: {command!r}")
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(command, proc.returncode, _cap(proc.stdout), _cap(proc.stderr))
    except subprocess.TimeoutExpired as exc:
        out = _cap(exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        err = _cap(exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        return CommandResult(command, -1, out, err, timed_out=True)
