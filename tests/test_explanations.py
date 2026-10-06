from __future__ import annotations

from datetime import date

from synaps_programplan.explanations import explain, explanation_gaps
from synaps_programplan.planner import SolveConfig, plan
from tests.conftest import program, stand, task, uses


def test_stored_explanation_matches_a_fresh_reading_of_the_plan() -> None:
    monday = date(2026, 10, 5)
    wednesday = date(2026, 10, 7)
    prog = program(
        [
            task(
                "a",
                3,
                "p1",
                demands=uses("st"),
                planned_start=monday,
                planned_finish=wednesday,
            ),
            task(
                "b",
                3,
                "p2",
                demands=uses("st"),
                planned_start=monday,
                planned_finish=wednesday,
            ),
        ],
        resources=[stand()],
    )
    result = plan(prog, SolveConfig(solver="greedy"))
    assert result.outcome.ok
    result.explanations = explain(prog, result)
    assert result.explanations
    assert explanation_gaps(prog, result) == []
    result.explanations[0].text = "работа сдвинута по другой причине"
    assert explanation_gaps(prog, result)
