from __future__ import annotations

from datetime import date

from synaps_programplan.edits import classify_churn
from synaps_programplan.model import FreezePolicy
from synaps_programplan.planner import SolveConfig, plan
from tests.conftest import program, stand, task, uses


def test_churn_weights_early_moves_and_counts_stand_order_and_freeze() -> None:
    monday = date(2026, 10, 5)
    prog = program(
        [task("a", 3, "p1", demands=uses("st")), task("b", 3, "p2", demands=uses("st"))],
        resources=[stand()],
    )
    base = plan(prog, SolveConfig(solver="greedy"))
    assert base.outcome.ok
    by_id = {row.task_id: row for row in base.tasks}
    moved = []
    for row in base.tasks:
        other = by_id["b" if row.task_id == "a" else "a"]
        moved.append(row.model_copy(update={"start_index": other.start_index, "reference_start": monday}))
    result = base.model_copy(update={"tasks": moved})
    frozen = prog.model_copy(update={"freeze": FreezePolicy(freeze_until=date(2026, 10, 20))})
    churn = classify_churn(frozen, base, result, set())
    assert churn["weighted_shift_wd"] > 0
    assert churn["stand_order_changes"] == 1
    assert churn["frozen_moved"] >= 1
