"""A manual move the working-day axis cannot hold is refused with a reason, never moved silently."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from synaps_programplan.edits import apply_moves, check_moves
from synaps_programplan.planner import SolveConfig, plan
from tests.conftest import START, dep, program, task


def _accepted():
    prog = program([task("a", 3), task("b", 2)], [dep("a", "b")])
    result = plan(prog, SolveConfig(solver="greedy"))
    assert result.outcome.ok
    return prog, result


def test_a_move_past_the_horizon_is_refused_with_a_reason() -> None:
    prog, result = _accepted()
    with pytest.raises(ValueError, match="горизонт"):
        check_moves(prog, result, {"b": date(2027, 12, 31)})


def test_a_move_before_the_status_date_is_refused_not_clamped() -> None:
    prog, result = _accepted()
    before = START - timedelta(days=7)
    with pytest.raises(ValueError, match="раньше даты статуса"):
        apply_moves(prog, result, {"b": before})


def test_a_move_inside_the_horizon_keeps_the_requested_working_day() -> None:
    prog, result = _accepted()
    monday = next(d for d in (START + timedelta(days=i) for i in range(14)) if d.weekday() == 0)
    rows = {row.task_id: row for row in apply_moves(prog, result, {"b": monday})}
    assert rows["b"].start == monday
