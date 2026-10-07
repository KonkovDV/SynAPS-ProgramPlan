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


def test_a_time_limited_difference_is_a_note_and_a_fixed_one_fails() -> None:
    import copy

    module = _script()
    recorded = json.loads((ROOT / "evidence.json").read_text(encoding="utf-8"))["demo"]
    moved_b = copy.deepcopy(recorded)
    moved_b["plans"]["B"]["finish"] = "2029-01-24"
    failures, notes = module.split_demo_differences(recorded, moved_b)
    assert failures == []
    assert notes and "demo.plans.B.finish" in notes[0]
    changed_e2 = copy.deepcopy(recorded)
    changed_e2["plans"]["E2"]["tardiness_wd"] = 5
    failures, _ = module.split_demo_differences(recorded, changed_e2)
    assert failures and "demo.plans.E2.tardiness_wd" in failures[0]
