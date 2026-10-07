"""Each hard bound of a task is its own witness requirement; relaxing one keeps the other."""

from __future__ import annotations

from datetime import date

from synaps_programplan.explanations import relax, relaxable_requirements
from tests.conftest import program, task


def test_a_task_with_two_hard_bounds_gives_two_requirements() -> None:
    prog = program([task("a", 5, deadline=date(2026, 10, 20), latest_finish=date(2026, 10, 30))])
    refs = [item.ref for item in relaxable_requirements(prog) if item.kind == "deadline"]
    assert sorted(refs) == ["a", "a#latest_finish"]


def test_relaxing_the_latest_finish_keeps_the_deadline() -> None:
    prog = program([task("a", 5, deadline=date(2026, 10, 20), latest_finish=date(2026, 10, 30))])
    latest = next(item for item in relaxable_requirements(prog) if item.ref == "a#latest_finish")
    relaxed = relax(prog, [latest]).task("a")
    assert relaxed.latest_finish is None
    assert relaxed.deadline == date(2026, 10, 20)
