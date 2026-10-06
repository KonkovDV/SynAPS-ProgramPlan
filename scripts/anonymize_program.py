"""Replace names with codes and shift every date by a constant.

The customer runs this on their own machine. The mapping is not written, and
the result must not be committed. Structure, durations and link types stay.

    python scripts/anonymize_program.py program.json --shift-days 100 --out anonymized.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_LABELS = {"name", "code", "org_unit", "owner"}


def anonymize(payload: dict[str, Any], shift_days: int) -> dict[str, Any]:
    """Return a new document. ``shift_days`` must not be zero."""
    if shift_days == 0:
        raise ValueError("shift-days must not be 0")
    codes: dict[str, str] = {}

    def label(text: str) -> str:
        if text not in codes:
            codes[text] = f"C{len(codes) + 1:04d}"
        return codes[text]

    def walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            return {item: walk(value, item) for item, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            shifted = _shift_date(node, shift_days)
            if shifted is not None:
                return shifted
            if key in _LABELS and node.strip():
                return label(node)
        return node

    walked = walk(payload)
    if not isinstance(walked, dict):
        raise ValueError("program JSON must be an object")
    return walked


def _shift_date(text: str, shift_days: int) -> str | None:
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return None
    moved = (day + timedelta(days=shift_days)).isoformat()
    return moved + text[10:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("program", type=Path)
    parser.add_argument("--shift-days", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = json.loads(args.program.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("program JSON must be an object")
    result = anonymize(payload, args.shift_days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
