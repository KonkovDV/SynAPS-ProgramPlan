"""Documents cite the measured record, and a hand-edited count is rejected."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("build_evidence", ROOT / "scripts" / "build_evidence.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_documents_cite_the_evidence_record() -> None:
    module = _script()
    assert module.check(None) == []


def test_a_hand_edited_test_count_is_reported() -> None:
    module = _script()
    gaps = module.pattern_gaps("Автоматических тестов — 1.\n", module.COUNT_PATTERNS["README.md"], 2)
    assert gaps


def test_show_directory_matches_the_evidence_record() -> None:
    demo = ROOT / "out" / "demo"
    if not demo.is_dir():
        return
    module = _script()
    evidence = json.loads((ROOT / "evidence.json").read_text(encoding="utf-8"))
    assert module.demo_record(demo) == evidence["demo"]
