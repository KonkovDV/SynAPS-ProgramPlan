from __future__ import annotations

from datetime import date

from synaps_programplan.explanations import explain, explanation_gaps, fact_errors
from synaps_programplan.planner import Adjustments, SolveConfig, plan
from synaps_programplan.result import CauseKind
from synaps_programplan.scenarios import WhatIf, program_for_plan, run_scenarios
from tests.conftest import dep, person, program, stand, task, uses


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


def test_what_if_reasons_are_read_from_the_changed_program() -> None:
    monday = date(2026, 10, 5)
    wednesday = date(2026, 10, 7)
    prog = program(
        [
            task("a", 3, "p1", demands=uses("st"), planned_start=monday, planned_finish=wednesday),
            task("b", 3, "p2", demands=uses("st"), planned_start=monday, planned_finish=wednesday),
        ],
        resources=[stand()],
    )
    bundle = run_scenarios(
        prog,
        SolveConfig(solver="greedy", time_limit_s=2),
        what_ifs=[WhatIf(label="второй стенд", add_capacity={"st": 1})],
    )
    found = bundle.by_id("E1")
    assert found.outcome.ok
    found.explanations = explain(prog, found)
    assert explanation_gaps(prog, found)
    solved = program_for_plan(prog, found)
    found.explanations = explain(solved, found)
    assert explanation_gaps(solved, found) == []
    assert explanation_gaps(prog, found)


def test_a_capacity_reserve_is_not_called_a_vacation() -> None:
    monday = date(2026, 10, 5)
    wednesday = date(2026, 10, 7)
    prog = program(
        [task("a", 3, demands=uses("eng", 10), planned_start=monday, planned_finish=wednesday)],
        resources=[person("eng")],
    )
    result = plan(prog, SolveConfig(solver="greedy"))
    assert result.outcome.ok and result.task("a").shift_wd == 0
    row = result.task("a")
    row.start_index += 5
    row.end_index += 5
    row.shift_wd = 5
    reasons = explain(prog, result, adjustments=Adjustments(capacity_scale={"eng": 0.8}))
    text = next(item.text for item in reasons if item.task_id == "a")
    assert "отпуск" not in text
    assert "резерв варианта" in text
    queued = program(
        [
            task("a", 3, "p1", demands=uses("eng", 10), planned_start=monday, planned_finish=wednesday),
            task("b", 3, "p2", demands=uses("eng", 10), planned_start=monday, planned_finish=wednesday),
        ],
        resources=[person("eng")],
    )
    both = plan(queued, SolveConfig(solver="greedy"))
    named = explain(queued, both, adjustments=Adjustments(capacity_scale={"eng": 0.8}))
    delayed = next(item for item in named if item.shift_wd > 0)
    assert delayed.cause_code == "RESOURCE_CONTENTION"
    assert delayed.fact is not None
    assert delayed.fact.kind is CauseKind.RESOURCE_CAPACITY
    assert "отпуск" not in delayed.text


def test_every_moved_task_has_a_fact_that_matches_the_plan() -> None:
    monday = date(2026, 10, 5)
    prog = program(
        [
            task("a", 3, planned_start=monday, planned_finish=date(2026, 10, 7)),
            task("b", 1, planned_start=monday, planned_finish=monday),
        ],
        [dep("a", "b")],
    )
    result = plan(prog, SolveConfig(solver="greedy"))
    assert result.outcome.ok
    result.explanations = explain(prog, result)
    moved = {row.task_id for row in result.tasks if row.shift_wd}
    assert moved
    assert moved <= {item.task_id for item in result.explanations if item.fact is not None}
    assert fact_errors(prog, result) == []
    held = next(item for item in result.explanations if item.task_id == "b")
    assert held.fact is not None
    assert held.fact.kind in (CauseKind.PRECEDENCE, CauseKind.MAX_LAG)
    assert held.fact.blocker_task_id == "a"
    assert CauseKind.SETUP_TRANSITION.value == "SETUP_TRANSITION"
    assert CauseKind.MODE_SELECTION.value == "MODE_SELECTION"
    held.fact.dates = ["1999-01-01"]
    assert any("1999-01-01" in item or "даты начала" in item for item in fact_errors(prog, result))
