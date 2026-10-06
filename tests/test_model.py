from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from synaps_programplan.model import Demand, TaskStatus, positive_cycle, skill_pool_conflicts
from tests.conftest import dep, needs, person, program, stand, task, uses


def test_milestone_cannot_demand_resources() -> None:
    with pytest.raises(ValidationError):
        program([task("m", 0, demands=uses("st"))], resources=[stand()])


def test_demand_needs_exactly_one_target() -> None:
    with pytest.raises(ValidationError):
        Demand(resource_id="a", skill_id="b", units=1)


def test_unknown_references_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        program([task("a", 2)], [dep("a", "zzz")])


def test_done_task_needs_actual_dates() -> None:
    with pytest.raises(ValidationError):
        task("a", 2, status=TaskStatus.DONE)


def test_zero_lag_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError):
        program([task("a", 2), task("b", 2)], [dep("a", "b"), dep("b", "a")])


def test_each_link_type_rejects_a_proven_positive_cycle() -> None:
    with pytest.raises(ValidationError, match="positive cycle"):
        program(
            [task("a", 5), task("b", 2)],
            [dep("a", "b", "FS", 0), dep("a", "b", "SS", 0, max_lag_wd=1)],
        )
    with pytest.raises(ValidationError, match="positive cycle"):
        program(
            [task("a", 5), task("b", 1)],
            [dep("a", "b", "FF", 0), dep("a", "b", "SS", 0, max_lag_wd=0)],
        )
    with pytest.raises(ValidationError, match="positive cycle"):
        program(
            [task("a", 5), task("b", 2)],
            [dep("a", "b", "FS", 0), dep("a", "b", "SF", 0, max_lag_wd=1)],
        )


def test_a_compensated_cycle_is_a_cycle_and_not_a_positive_one() -> None:
    from synaps_programplan.model import OKRProgram

    links = [dep("a", "b", "SS", 0), dep("b", "a", "SS", 0)]
    skipped = OKRProgram.model_construct(
        tasks=[task("a", 2), task("b", 2)],
        dependencies=links,
    )
    assert positive_cycle(skipped) == []
    with pytest.raises(ValidationError, match="dependency graph has a cycle"):
        program([task("a", 2), task("b", 2)], links)


def test_max_lag_cycle_detects_positive_cycle() -> None:
    prog = program([task("a", 5), task("b", 2)], [dep("a", "b", "FS", 0, max_lag_wd=10)])
    assert positive_cycle(prog) == []
    contradictory = [
        dep("a", "b"),  # b starts >= 5 after a
        dep("a", "c", "SS", 0, max_lag_wd=1),
        dep("c", "b", "SS", 0, max_lag_wd=2),  # ... but at most 3 after a
    ]
    with pytest.raises(ValidationError, match="positive cycle"):
        program([task("a", 5), task("b", 2), task("c", 1)], contradictory)


def test_skill_pool_conflicts() -> None:
    clean = program([task("a", 2, demands=needs("d"))], resources=[person("x", ["d"])], skills=["d"])
    assert skill_pool_conflicts(clean) == []
    overlapping = program(
        [task("a", 2, demands=needs("d")), task("b", 2, demands=needs("e"))],
        resources=[person("x", ["d", "e"])],
        skills=["d", "e"],
    )
    assert skill_pool_conflicts(overlapping) == ["x"]


@given(
    duration=st.integers(min_value=1, max_value=8),
    lag=st.integers(min_value=0, max_value=4),
    slack=st.integers(min_value=0, max_value=4),
    kind=st.sampled_from(["FS", "SS", "FF", "SF"]),
)
@settings(max_examples=30, deadline=None)
def test_one_max_lag_on_two_tasks_stays_feasible(duration: int, lag: int, slack: int, kind: str) -> None:
    prog = program(
        [task("a", duration), task("b", duration)],
        [dep("a", "b", kind, lag, max_lag_wd=lag + slack)],
    )
    assert positive_cycle(prog) == []


def test_horizon_before_the_calendar_origin_is_refused() -> None:
    from synaps_programplan.model import Calendar, OKRProgram, Program, Project

    with pytest.raises(ValidationError, match="2000-01-03"):
        OKRProgram(
            program=Program(
                id="prog",
                name="test",
                calendar_id="cal",
                horizon_start=date(1999, 1, 4),
                horizon_end=date(1999, 6, 1),
                status_date=date(1999, 1, 4),
            ),
            calendars=[Calendar(id="cal")],
            projects=[Project(id="p1", code="P1", name="p1")],
            tasks=[task("a", 2)],
        )


def test_reference_dates_prefer_baseline() -> None:
    prog = program([task("a", 2, planned_start=date(2026, 10, 5), planned_finish=date(2026, 10, 6))])
    assert prog.reference_dates()["a"] == (date(2026, 10, 5), date(2026, 10, 6))
