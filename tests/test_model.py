from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from synaps_okrplan.model import Demand, TaskStatus, positive_cycle, skill_pool_conflicts
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


def test_reference_dates_prefer_baseline() -> None:
    prog = program([task("a", 2, planned_start=date(2026, 10, 5), planned_finish=date(2026, 10, 6))])
    assert prog.reference_dates()["a"] == (date(2026, 10, 5), date(2026, 10, 6))
