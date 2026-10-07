"""A stand keeps a changeover interval between states. The interval is not added to the task."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from synaps_programplan.checker import check_plan
from synaps_programplan.compiler import compile_program
from synaps_programplan.explanations import explain, fact_errors
from synaps_programplan.model import (
    Changeover,
    ExecutionMode,
    Product,
    ProductConfiguration,
    StandState,
)
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.result import CauseKind, Claim
from tests.conftest import program, stand, task, uses

START = date(2026, 10, 5)


def _states() -> list[StandState]:
    return [
        StandState(id="alpha", resource_id="st", code="ALPHA"),
        StandState(id="beta", resource_id="st", code="BETA"),
    ]


def _matrix() -> list[Changeover]:
    return [
        Changeover(resource_id="st", from_state_id="alpha", to_state_id="beta", duration_wd=4),
        Changeover(resource_id="st", from_state_id="beta", to_state_id="alpha", duration_wd=4),
    ]


def _job(task_id: str, state_id: str):
    return task(
        task_id,
        2,
        demands=uses("st"),
        stand_state_id=state_id,
        planned_start=START,
        planned_finish=date(2026, 10, 6),
    )


def _bench():
    jobs = [_job("a", "alpha"), _job("c", "alpha"), _job("b", "beta")]
    return program(jobs, resources=[stand()], stand_states=_states(), changeovers=_matrix())


def test_the_stand_groups_one_state_and_keeps_the_changeover_gap() -> None:
    prog = _bench()
    result = plan(prog, SolveConfig(time_limit_s=10))
    assert result.outcome.claim is Claim.OPTIMAL
    assert check_plan(prog, result.tasks) == []
    ordered = sorted(result.tasks, key=lambda row: row.start_index)
    states = [prog.task(row.task_id).stand_state_id for row in ordered]
    assert states in (["alpha", "alpha", "beta"], ["beta", "alpha", "alpha"])
    for previous, nxt in zip(ordered, ordered[1:], strict=False):
        same = prog.task(previous.task_id).stand_state_id == prog.task(nxt.task_id).stand_state_id
        assert nxt.start_index - previous.end_index == (0 if same else 4)
    result.explanations = explain(prog, result)
    assert fact_errors(prog, result) == []
    told = [
        item for item in result.explanations if item.fact and item.fact.kind is CauseKind.SETUP_TRANSITION
    ]
    assert told
    assert "переналадка" in told[0].text


def test_a_missing_changeover_and_an_overlap_are_rejected() -> None:
    prog = _bench()
    result = plan(prog, SolveConfig(time_limit_s=10))
    ordered = sorted(result.tasks, key=lambda row: row.start_index)
    previous, nxt = next(
        (left, right)
        for left, right in zip(ordered, ordered[1:], strict=False)
        if prog.task(left.task_id).stand_state_id != prog.task(right.task_id).stand_state_id
    )
    compiled = compile_program(prog)
    start = compiled.axis.start_date(previous.end_index)
    finish = compiled.axis.finish_date(previous.end_index, previous.end_index + nxt.duration_wd)
    forged = [
        row.model_copy(update={"start": start, "finish": finish}) if row.task_id == nxt.task_id else row
        for row in result.tasks
    ]
    codes = {item.code for item in check_plan(prog, forged)}
    assert "CHANGEOVER_SHORT" in codes
    assert "CHANGEOVER_OVERLAP" in codes


def test_an_incompatible_configuration_is_refused() -> None:
    product = Product(id="eng", project_id="p1", code="E", name="E")
    configs = [
        ProductConfiguration(id="cfg-a", product_id="eng", code="A", name="A"),
        ProductConfiguration(id="cfg-b", product_id="eng", code="B", name="B"),
    ]
    hot = StandState(id="hot", resource_id="st", code="HOT", configuration_id="cfg-a")
    with pytest.raises(ValidationError, match="incompatible"):
        program(
            [task("a", 2, demands=uses("st"), stand_state_id="hot", configuration_id="cfg-b")],
            resources=[stand()],
            products=[product],
            configurations=configs,
            stand_states=[hot],
            changeovers=[Changeover(resource_id="st", from_state_id="hot", to_state_id="hot", duration_wd=1)],
        )


def test_a_mode_on_another_task_does_not_block_the_stand() -> None:
    other = task(
        "m",
        2,
        modes=[
            ExecutionMode(code="fast", duration_wd=2),
            ExecutionMode(code="slow", duration_wd=5),
        ],
    )
    jobs = [_job("a", "alpha"), _job("c", "alpha"), _job("b", "beta"), other]
    prog = program(jobs, resources=[stand()], stand_states=_states(), changeovers=_matrix())
    result = plan(prog, SolveConfig(time_limit_s=10))
    assert result.outcome.ok
    chosen = next(row for row in result.tasks if row.task_id == "m")
    assert chosen.mode_code == "fast"


def test_modes_and_changeover_on_one_task_are_refused() -> None:
    with pytest.raises(ValidationError, match="modes"):
        program(
            [
                task(
                    "a",
                    2,
                    modes=[ExecutionMode(code="fast", duration_wd=2, demands=uses("st"))],
                )
            ],
            resources=[stand()],
            stand_states=_states(),
            changeovers=_matrix(),
        )


def test_a_program_without_changeover_omits_the_new_fields() -> None:
    dumped = program([task("a", 2)]).model_dump(mode="json")
    assert "stand_states" not in dumped
    assert "changeovers" not in dumped
    assert "stand_state_id" not in dumped["tasks"][0]
