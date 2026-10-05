from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from synaps_programplan.cli import main
from synaps_programplan.io import load_plan, save_program
from synaps_programplan.io.mspdi import read_mspdi
from synaps_programplan.journal import append_decision
from tests.conftest import dep, program, stand, task, uses


def _solve(tmp_path: Path, deadline: date | None) -> tuple[Path, Path, int]:
    prog = program(
        [task("a", 5, demands=uses("st")), task("b", 3, demands=uses("st"), deadline=deadline)],
        [dep("a", "b")],
        resources=[stand()],
    )
    program_path, plan_path = tmp_path / "program.json", tmp_path / "plan.json"
    save_program(prog, program_path)
    code = main(["solve", str(program_path), "--out", str(plan_path), "--time-limit", "5"])
    return program_path, plan_path, code


def test_export_writes_an_accepted_plan(tmp_path: Path) -> None:
    program_path, plan_path, code = _solve(tmp_path, None)
    assert code == 0
    out = tmp_path / "plan.xml"
    assert main(["export", str(program_path), str(plan_path), "--out", str(out)]) == 0
    assert len(read_mspdi(out, code="x").tasks) == 2


def test_export_refuses_a_plan_that_was_not_accepted(tmp_path: Path) -> None:
    program_path, plan_path, code = _solve(tmp_path, date(2026, 10, 7))
    assert code == 1
    out = tmp_path / "plan.xml"
    assert main(["export", str(program_path), str(plan_path), "--out", str(out)]) == 1
    assert not out.exists()


def test_check_and_replan_with_moves(tmp_path: Path) -> None:
    program_path, plan_path, code = _solve(tmp_path, None)
    assert code == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"scenario": "base", "moves": {"b": "2026-10-05"}}), encoding="utf-8")
    assert main(["check", str(program_path), str(plan_path), "--moves", str(bad)]) == 1
    late = tmp_path / "late.json"
    late.write_text(json.dumps({"moves": {"a": "2026-10-12"}}), encoding="utf-8")
    assert main(["check", str(program_path), str(plan_path), "--moves", str(late)]) == 1
    out = tmp_path / "plan_R1.json"
    assert main(["replan", str(program_path), str(plan_path), "--moves", str(late), "--out", str(out)]) == 0
    replanned = load_plan(out)
    assert replanned.outcome.ok and replanned.task("a").start == date(2026, 10, 12)
    assert replanned.task("b").start == date(2026, 10, 19)


def test_report_with_risk_and_journal_command(tmp_path: Path) -> None:
    program_path, plan_path, _ = _solve(tmp_path, None)
    out = tmp_path / "report.html"
    args = ["report", str(program_path), str(plan_path), "--risk-runs", "20", "--out", str(out)]
    assert main(args) == 0
    text = out.read_text(encoding="utf-8")
    assert '"risk":{' in text and "riskCard" in text
    journal = tmp_path / "j.jsonl"
    append_decision(
        journal, action="accept", user="u", role="manager", scenario_id="base", plan_hash="h", input_hash="i"
    )
    assert main(["journal", str(journal)]) == 0
    journal.write_text(journal.read_text(encoding="utf-8").replace('"u"', '"x"'), encoding="utf-8")
    assert main(["journal", str(journal)]) == 1


def test_serve_refuses_an_open_host_without_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program_path, plan_path, _ = _solve(tmp_path, None)
    monkeypatch.delenv("SYNAPS_PROGRAMPLAN_TOKENS", raising=False)
    assert main(["serve", str(program_path), str(plan_path), "--host", "0.0.0.0"]) == 2


def test_stability_objective_is_accepted_by_the_command(tmp_path: Path) -> None:
    prog = program([task("a", 2, planned_start=date(2026, 10, 12), planned_finish=date(2026, 10, 13))])
    program_path, plan_path = tmp_path / "program.json", tmp_path / "plan.json"
    save_program(prog, program_path)
    args = ["solve", str(program_path), "--out", str(plan_path), "--objective", "stability"]
    assert main(args) == 0
