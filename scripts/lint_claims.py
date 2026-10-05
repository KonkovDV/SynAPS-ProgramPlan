"""Fail if a banned claim appears outside the registry that defines it."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"CLAIMS_REGISTRY.md", "BANNED_CLAIMS.txt", "lint_claims.py"}
SUFFIXES = {".py", ".md", ".html", ".toml"}


def main() -> int:
    banned = [
        line.strip()
        for line in (ROOT / "BANNED_CLAIMS.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SUFFIXES or path.name in SKIP:
            continue
        if any(part in {".venv", "out", ".mypy_cache", ".ruff_cache"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for phrase in banned:
            if phrase in text:
                hits.append(f"{path.relative_to(ROOT)}: {phrase}")
    if hits:
        sys.stderr.write("banned claims found:\n" + "\n".join(hits) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
