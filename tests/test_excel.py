from __future__ import annotations

from pathlib import Path

from synaps_programplan.io.excel import read_excel, write_excel, write_template
from tests.conftest import dep, needs, person, program, stand, task, uses


def test_excel_roundtrip(tmp_path: Path) -> None:
    original = program(
        [
            task("a", 3, demands=uses("st") + needs("design")),
            task("m", 0),
        ],
        [dep("a", "m", "FS", 1)],
        resources=[stand(), person("ivanov", ["design"])],
        skills=["design"],
    )
    path = tmp_path / "program.xlsx"
    write_excel(original, path)
    loaded = read_excel(path)
    assert [(t.id, t.duration_wd, t.project_id) for t in loaded.tasks] == [
        ("a", 3, "p1"),
        ("m", 0, "p1"),
    ]
    assert loaded.tasks[0].demands[0].resource_id == "st"
    assert loaded.tasks[0].demands[1].skill_id == "design"
    assert loaded.dependencies[0].lag_wd == 1
    assert {r.id for r in loaded.resources} == {"st", "ivanov"}
    assert loaded.resources[1].skills == ["design"]


def test_template_has_no_data_rows(tmp_path: Path) -> None:
    path = tmp_path / "template.xlsx"
    write_template(path)
    try:
        read_excel(path)
    except ValueError as exc:
        assert "Program" in str(exc)
    else:
        raise AssertionError("comment row was imported as data")
