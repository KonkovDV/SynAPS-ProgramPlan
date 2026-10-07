"""A task can be done in more than one way. CP-SAT keeps exactly one."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from synaps_programplan.checker import check_plan
from synaps_programplan.explanations import explain
from synaps_programplan.model import ExecutionMode, Task, TaskStatus
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.result import CauseKind, Claim, TaskPlan
from tests.conftest import program, stand, task, uses


def _choice() -> Task:
    return task(
        "pick",
        2,
        modes=[
            ExecutionMode(code="slow", duration_wd=8),
            ExecutionMode(code="fast", duration_wd=2, demands=uses("st")),
        ],
    )


def test_the_shorter_mode_is_chosen_when_the_stand_is_free() -> None:
    prog = program([_choice()], resources=[stand()])
    result = plan(prog, SolveConfig(time_limit_s=10))
    assert result.outcome.claim is Claim.OPTIMAL
    row = result.tasks[0]
    assert row.mode_code == "fast"
    assert row.duration_wd == 2
    assert row.demand_ids == ["st"]
    assert check_plan(prog, result.tasks) == []
    told = explain(prog, result)
    assert told[0].fact is not None
    assert told[0].fact.kind is CauseKind.MODE_SELECTION
    assert told[0].fact.mode_code == "fast"
    assert "fast" in told[0].text


def test_the_mode_that_avoids_a_busy_stand_finishes_earlier() -> None:
    holder = task("hold", 5, demands=uses("st"))
    chooser = task(
        "pick",
        2,
        modes=[
            ExecutionMode(code="fast", duration_wd=2, demands=uses("st")),
            ExecutionMode(code="alone", duration_wd=4),
        ],
    )
    prog = program([holder, chooser], resources=[stand()])
    result = plan(prog, SolveConfig(time_limit_s=10))
    assert result.outcome.ok
    row = next(item for item in result.tasks if item.task_id == "pick")
    holder = next(item for item in result.tasks if item.task_id == "hold")
    assert row.mode_code == "alone"
    assert row.duration_wd == 4
    assert row.finish <= holder.finish
    assert result.kpi is not None
    assert result.kpi.program_finish == holder.finish


def test_a_missing_mode_and_foreign_resources_are_rejected() -> None:
    prog = program([_choice()], resources=[stand()])
    result = plan(prog, SolveConfig(time_limit_s=10))
    row = result.tasks[0]
    unknown = row.model_copy(update={"mode_code": "nope"})
    assert "MODE_UNKNOWN" in {item.code for item in check_plan(prog, [unknown])}
    foreign = row.model_copy(update={"demand_ids": []})
    assert "MODE_DEMAND" in {item.code for item in check_plan(prog, [foreign])}


def test_greedy_refuses_modes() -> None:
    result = plan(program([_choice()], resources=[stand()]), SolveConfig(solver="greedy", time_limit_s=5))
    assert result.outcome.claim is Claim.UNSUPPORTED_MODEL
    assert result.outcome.ok is False
    assert result.tasks == []


def test_modes_stay_out_of_a_plan_that_has_none() -> None:
    assert "modes" not in task("a", 3).model_dump(mode="json")
    dumped = TaskPlan.model_validate(
        {
            "task_id": "a",
            "project_id": "p1",
            "name": "a",
            "start": "2026-10-05",
            "finish": "2026-10-07",
            "duration_wd": 3,
            "status": TaskStatus.PLANNED,
            "is_milestone": False,
            "start_index": 0,
            "end_index": 3,
        }
    ).model_dump(mode="json")
    assert "mode_code" not in dumped
    assert "demand_ids" not in dumped


def test_a_mode_task_must_name_its_shortest_duration() -> None:
    with pytest.raises(ValidationError):
        task(
            "a",
            5,
            modes=[ExecutionMode(code="fast", duration_wd=2), ExecutionMode(code="slow", duration_wd=8)],
        )
