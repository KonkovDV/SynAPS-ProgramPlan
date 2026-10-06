from __future__ import annotations

import json
from pathlib import Path

import pytest

from synaps_programplan.cli import main
from synaps_programplan.io import save_plan, save_program
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.stand import demo_checks, environment_checks, stand_ok
from tests.conftest import dep, program, stand, task, uses


def _demo(tmp_path: Path) -> Path:
    prog = program(
        [task("a", 5, demands=uses("st")), task("b", 3, demands=uses("st"))],
        [dep("a", "b")],
        resources=[stand()],
    )
    result = plan(prog, SolveConfig(solver="greedy"), scenario_id="A", label="A")
    assert result.outcome.ok
    save_program(prog, tmp_path / "program.json")
    save_plan(result, tmp_path / "plan_A.json")
    stamp = str(result.evidence["plan_hash"])
    (tmp_path / "report.html").write_text(f"<html>{stamp}</html>", encoding="utf-8")
    for name in ("report_infeasible.html", "plan.xml", "risk_A.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    return tmp_path


def test_a_complete_demo_directory_is_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SYNAPS_PROGRAMPLAN_YANDEX_API_KEY", "SYNAPS_PROGRAMPLAN_YANDEX_FOLDER"):
        monkeypatch.delenv(name, raising=False)
    checks = environment_checks() + demo_checks(_demo(tmp_path))
    assert stand_ok(checks), [item.as_dict() for item in checks if not item.ok]


def test_a_rewritten_plan_date_blocks_the_stand(tmp_path: Path) -> None:
    root = _demo(tmp_path)
    data = json.loads((root / "plan_A.json").read_text(encoding="utf-8"))
    data["tasks"][0]["finish"] = "2030-01-01"
    (root / "plan_A.json").write_text(json.dumps(data), encoding="utf-8")
    found = {item.name: item for item in demo_checks(root)}
    assert not found["plan:A"].ok
    assert "plan_hash" in found["plan:A"].detail


def test_a_missing_report_blocks_the_stand(tmp_path: Path) -> None:
    root = _demo(tmp_path)
    (root / "report.html").unlink()
    assert not stand_ok(demo_checks(root))


def test_a_configured_language_model_blocks_the_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_YANDEX_API_KEY", "k")
    found = {item.name: item for item in environment_checks()}
    assert not found["language_model_off"].ok
    assert not stand_ok(list(found.values()))


def test_doctor_exit_code_follows_the_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SYNAPS_PROGRAMPLAN_YANDEX_API_KEY", "SYNAPS_PROGRAMPLAN_YANDEX_FOLDER"):
        monkeypatch.delenv(name, raising=False)
    root = _demo(tmp_path)
    assert main(["doctor", "--demo", str(root)]) == 0
    (root / "plan.xml").unlink()
    assert main(["doctor", "--demo", str(root)]) == 1
