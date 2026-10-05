from __future__ import annotations

from datetime import date
from pathlib import Path

from synaps_programplan.cli import main
from synaps_programplan.io import save_program
from synaps_programplan.io.mspdi import read_mspdi
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


def test_stability_objective_is_accepted_by_the_command(tmp_path: Path) -> None:
    prog = program([task("a", 2, planned_start=date(2026, 10, 12), planned_finish=date(2026, 10, 13))])
    program_path, plan_path = tmp_path / "program.json", tmp_path / "plan.json"
    save_program(prog, program_path)
    args = ["solve", str(program_path), "--out", str(plan_path), "--objective", "stability"]
    assert main(args) == 0
