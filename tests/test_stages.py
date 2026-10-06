from __future__ import annotations

from pathlib import Path

from synaps_programplan.enterprises import enterprise_rollups
from synaps_programplan.io.excel import read_excel, write_excel
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.stages import stage_catalog
from synaps_programplan.synthetic import SyntheticSpec, generate
from tests.conftest import program, stand, task, uses


def test_stage_catalog_is_the_config_file() -> None:
    stages = stage_catalog()
    assert [stage.code for stage in stages] == ["TZ", "EP", "TP", "RKD", "OO", "PI", "GI"]
    assert stages[-1].test is True
    built = generate(SyntheticSpec(projects=1, tasks_per_stage=1, seed=1))
    assert {node.id for node in built.wbs} == {f"okr1.{stage.code}" for stage in stages}


def test_enterprise_rolls_up_dates_and_stand_demand(tmp_path: Path) -> None:
    prog = program(
        [task("a", 3, "p1", demands=uses("st")), task("b", 2, "p2", demands=uses("st"))],
        resources=[stand()],
    )
    plants = prog.model_copy(
        update={
            "projects": [row.model_copy(update={"enterprise": "Салют"}) for row in prog.projects],
        }
    )
    result = plan(plants, SolveConfig(solver="greedy"))
    assert result.outcome.ok
    rows = enterprise_rollups(plants, result)
    assert len(rows) == 1
    row = rows[0]
    assert row["enterprise"] == "Салют"
    assert row["projects"] == ["p1", "p2"]
    assert row["tasks"] == 2
    assert row["start"] == min(result.task("a").start, result.task("b").start)
    assert row["finish"] == max(result.task("a").finish, result.task("b").finish)
    assert row["stand_demand_wd"] == 5
    path = tmp_path / "program.xlsx"
    write_excel(plants, path)
    assert read_excel(path).projects[0].enterprise == "Салют"
