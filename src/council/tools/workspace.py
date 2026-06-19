"""Per-run workspace: file read/write strictly confined to one directory.

Every path is resolved and checked to live under the workspace root, so an
agent cannot read or write outside its sandbox via ``..`` or absolute paths.
"""
from __future__ import annotations

from pathlib import Path


class PathEscape(ValueError):
    """Raised when a requested path resolves outside the workspace root."""


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel: str) -> Path:
        # Reject absolute inputs outright; join + resolve, then confirm containment.
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PathEscape(f"path {rel!r} escapes workspace {self.root}")
        return candidate

    def write_file(self, rel: str, content: str) -> str:
        path = self._resolve(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path.relative_to(self.root))

    def read_file(self, rel: str) -> str:
        path = self._resolve(rel)
        if not path.is_file():
            raise FileNotFoundError(rel)
        return path.read_text()

    def list_files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root))
            for p in self.root.rglob("*")
            if p.is_file()
        )
