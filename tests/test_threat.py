from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synaps_programplan.api import app
from synaps_programplan.io.excel import _refuse_expanding_workbook, read_excel, write_template
from synaps_programplan.isolate import plan_isolated
from synaps_programplan.planner import SolveConfig
from tests.conftest import dep, program, task

client = TestClient(app)


def test_too_many_resources_and_a_long_horizon_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from synaps_programplan.model import OKRProgram

    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_MAX_RESOURCES", "1")
    wide = program([task("a", 2)], resources=[])
    body = wide.model_dump(mode="json")
    body["resources"] = [
        {"id": "s1", "kind": "STAND", "code": "S1", "name": "s1", "capacity_units": 1},
        {"id": "s2", "kind": "STAND", "code": "S2", "name": "s2", "capacity_units": 1},
    ]
    with pytest.raises(ValueError, match="limit is 1"):
        OKRProgram.model_validate(body)
    short = program([task("a", 2)]).model_dump(mode="json")
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_MAX_HORIZON_DAYS", "10")
    with pytest.raises(ValueError, match="horizon is"):
        OKRProgram.model_validate(short)


def test_too_many_tasks_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    prog = program([task("a", 2), task("b", 2)], [dep("a", "b")])
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_MAX_TASKS", "1")
    response = client.post("/solve", json={"program": prog.model_dump(mode="json"), "solver": "greedy"})
    assert response.status_code == 422
    assert "limit is 1" in response.json()["detail"]


def test_an_expanding_workbook_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("synaps_programplan.io.excel.MAX_XLSX_UNCOMPRESSED", 50)
    path = tmp_path / "wide.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", b"A" * 5000)
    with pytest.raises(ValueError, match="expands too far"):
        read_excel(path)


def test_a_normal_template_passes_the_zip_check(tmp_path: Path) -> None:
    path = tmp_path / "program.xlsx"
    write_template(path)
    _refuse_expanding_workbook(path)


def test_solver_process_stops_when_memory_is_tiny() -> None:
    prog = program([task("a", 2)])
    with pytest.raises(MemoryError):
        plan_isolated(prog, SolveConfig(solver="greedy", time_limit_s=5), memory_mb=1)
