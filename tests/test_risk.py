from __future__ import annotations

from datetime import date

from synaps_programplan.montecarlo import simulate
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.quality import quality_report
from tests.conftest import dep, program, stand, task, uses


def test_fixed_durations_match_the_chain() -> None:
    prog = program([task("a", 3), task("m", 0), task("b", 2)], [dep("a", "m"), dep("m", "b")])
    accepted = plan(prog, SolveConfig(time_limit_s=5))
    assert accepted.outcome.ok
    risk = simulate(prog, accepted, runs=8, seed=1, low_factor=1, high_factor=1)
    assert risk.scheduled_runs == 8
    assert risk.program_finish["p50"] == risk.program_finish["p90"]
    milestone = next(item for item in risk.milestone_risk if item.task_id == "m")
    assert milestone.p50 == date(2026, 10, 7)
    assert risk.program_finish["p50"] == date(2026, 10, 9)
    assert risk.criticality["a"] == 1.0
    assert risk.criticality["b"] == 1.0


def test_same_seed_same_quantiles() -> None:
    prog = program(
        [task("a", 4, demands=uses("st")), task("b", 4, "p2", demands=uses("st"))],
        resources=[stand()],
    )
    accepted = plan(prog, SolveConfig(solver="greedy"))
    first = simulate(prog, accepted, runs=30, seed=3)
    second = simulate(prog, accepted, runs=30, seed=3)
    assert first.as_dict() == second.as_dict()
    assert first.program_finish["p90"] >= first.program_finish["p50"]


def test_rejected_plan_is_not_a_risk_baseline() -> None:
    prog = program([task("a", 5, deadline=date(2026, 10, 6))])
    rejected = plan(prog, SolveConfig(time_limit_s=5))
    assert not rejected.outcome.ok
    try:
        simulate(prog, rejected, runs=2)
    except ValueError as exc:
        assert "outcome.ok" in str(exc)
    else:
        raise AssertionError("risk accepted a rejected plan")


def test_dangling_task_is_a_quality_warning() -> None:
    found = quality_report(program([task("alone", 2)]))
    codes = [item["code"] for item in found["issues"]]
    assert "DANGLING" in codes
    assert "NO_RESOURCES" in codes
