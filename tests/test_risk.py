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


def test_max_lag_link_keeps_its_minimum_lag_in_the_simulation() -> None:
    prog = program(
        [task("a", 3), task("b", 2), task("m", 0)],
        [dep("a", "b", lag=4, max_lag_wd=6), dep("b", "m")],
    )
    accepted = plan(prog, SolveConfig(time_limit_s=5))
    assert accepted.outcome.ok and accepted.kpi is not None
    risk = simulate(prog, accepted, runs=4, seed=1, low_factor=1, high_factor=1)
    assert risk.program_finish["p50"] == accepted.kpi.program_finish
    milestone = next(item for item in risk.milestone_risk if item.task_id == "m")
    assert milestone.p50 == accepted.task("m").finish


def test_a_draw_that_breaks_a_max_lag_is_not_a_sample() -> None:
    from synaps_programplan.model import RiskDriver

    prog = program(
        [task("a", 1), task("c", 1), task("b", 1)],
        [dep("a", "b", lag=0, max_lag_wd=2), dep("c", "b")],
        risk_drivers=[
            RiskDriver(id="R", name="stretch", probability=1, low=5, mode=5, high=5, task_ids=["c"])
        ],
    )
    accepted = plan(prog, SolveConfig(time_limit_s=5))
    assert accepted.outcome.ok
    nominal = simulate(prog, accepted, runs=4, seed=1, low_factor=1, high_factor=1)
    # The driver is certain, so every draw stretches c past the max lag from a.
    without = prog.model_copy(update={"risk_drivers": []})
    quiet = simulate(without, accepted, runs=4, seed=1, low_factor=1, high_factor=1)
    assert quiet.scheduled_runs == 4
    assert nominal.scheduled_runs == 0


def test_grouped_drivers_occur_together() -> None:
    from synaps_programplan.model import RiskDriver
    from synaps_programplan.montecarlo import _occurrence_draws

    drivers = [
        RiskDriver(id="A", name="a", probability=0.5, task_ids=["a"], group="storm"),
        RiskDriver(id="B", name="b", probability=0.5, task_ids=["b"], group="storm"),
        RiskDriver(id="C", name="c", probability=0.5, task_ids=["a"]),
    ]
    rng = __import__("random").Random(1)
    disagree = 0
    for _ in range(200):
        hit = _occurrence_draws(drivers, rng)
        if hit["A"] != hit["B"]:
            disagree += 1
    assert disagree == 0


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


def _driver_program(probability: float) -> tuple[object, object]:
    from synaps_programplan.model import RiskDriver

    prog = program(
        [task("a", 10), task("b", 10), task("m", 0, deadline=date(2026, 10, 30))],
        [dep("a", "b"), dep("b", "m")],
        risk_drivers=[
            RiskDriver(
                id="R1", name="retest", probability=probability, low=1.5, mode=2.0, high=2.5, task_ids=["b"]
            ),
            RiskDriver(id="R0", name="noise", probability=0.5, low=1.0, mode=1.0, high=1.01, task_ids=["a"]),
        ],
    )
    accepted = plan(prog, SolveConfig(time_limit_s=5))
    assert accepted.outcome.ok
    return prog, accepted


def test_risk_drivers_are_ranked_by_p80_gain() -> None:
    prog, accepted = _driver_program(1.0)
    risk = simulate(prog, accepted, runs=40, seed=5, low_factor=1, high_factor=1)  # type: ignore[arg-type]
    assert [d.driver_id for d in risk.drivers] == ["R1", "R0"]
    top = risk.drivers[0]
    assert top.occurred_share == 1.0
    assert top.p80_gain_wd >= 5
    assert risk.drivers[1].p80_gain_wd == 0
    milestone = next(m for m in risk.milestone_risk if m.task_id == "m")
    assert milestone.hard and milestone.on_time_share == 0.0
    assert risk.deadlines_met_share == 0.0
    assert risk.as_dict()["drivers"][0]["driver_id"] == "R1"


def test_driver_that_never_fires_changes_nothing() -> None:
    prog, accepted = _driver_program(1e-9)
    risk = simulate(prog, accepted, runs=20, seed=2, low_factor=1, high_factor=1)  # type: ignore[arg-type]
    assert all(d.p80_gain_wd == 0 for d in risk.drivers)
    milestone = next(m for m in risk.milestone_risk if m.task_id == "m")
    assert milestone.on_time_share == 1.0


def test_unknown_task_in_a_risk_driver_is_rejected() -> None:
    import pytest

    from synaps_programplan.model import RiskDriver

    with pytest.raises(ValueError, match="unknown task"):
        program([task("a", 1)], risk_drivers=[RiskDriver(id="R", name="x", probability=0.5, task_ids=["zz"])])
    with pytest.raises(ValueError, match="low <= mode <= high"):
        RiskDriver(id="R", name="x", probability=0.5, low=2, mode=1, high=3, task_ids=["a"])


def test_dropping_a_project_prunes_its_risk_drivers() -> None:
    from synaps_programplan.model import RiskDriver
    from synaps_programplan.scenarios import WhatIf, apply_what_if

    prog = program(
        [task("a", 2), task("b", 2, "p2")],
        risk_drivers=[
            RiskDriver(id="R1", name="x", probability=0.5, task_ids=["a", "b"]),
            RiskDriver(id="R2", name="y", probability=0.5, task_ids=["b"]),
        ],
    )
    reduced = apply_what_if(prog, WhatIf(label="без p2", drop_projects=["p2"]))
    assert [(d.id, d.task_ids) for d in reduced.risk_drivers] == [("R1", ["a"])]


def test_zero_uncertainty_reproduces_the_accepted_plan() -> None:
    from synaps_programplan.synthetic import SyntheticSpec, generate

    prog = generate(SyntheticSpec(projects=2, seed=3)).model_copy(update={"risk_drivers": []})
    accepted = plan(prog, SolveConfig(solver="greedy"))
    assert accepted.outcome.ok and accepted.kpi is not None
    risk = simulate(prog, accepted, runs=2, seed=1, low_factor=1, high_factor=1)
    assert risk.program_finish["p50"] == accepted.kpi.program_finish
    for item in risk.milestone_risk:
        assert item.p50 == accepted.task(item.task_id).finish


def test_earliest_start_bounds_the_simulation() -> None:
    prog = program([task("a", 2, earliest_start=date(2026, 10, 19))])
    accepted = plan(prog, SolveConfig(time_limit_s=5))
    risk = simulate(prog, accepted, runs=5, seed=1, low_factor=1, high_factor=1)
    assert risk.program_finish["p50"] == date(2026, 10, 20)


def test_dangling_task_is_a_quality_warning() -> None:
    found = quality_report(program([task("alone", 2)]))
    codes = [item["code"] for item in found["issues"]]
    assert "DANGLING" in codes
    assert "NO_RESOURCES" in codes
