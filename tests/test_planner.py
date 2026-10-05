from __future__ import annotations

from datetime import date

import pytest
from synaps.model import SolverStatus

from synaps_programplan.checker import check_plan
from synaps_programplan.model import CapacityException, ExceptionReason, TaskStatus
from synaps_programplan.planner import SolveConfig, _claim, plan
from synaps_programplan.result import Claim, PlanResult, Severity
from tests.conftest import dep, needs, person, program, stand, task, uses

FAST = SolveConfig(time_limit_s=10)


def _accepted(result: PlanResult) -> PlanResult:
    assert result.outcome.ok, (result.outcome, [v.message for v in result.violations])
    assert result.outcome.kernel_verified
    hard = [v for v in result.violations if v.severity is Severity.HARD]
    assert not hard
    return result


def _start(result: PlanResult, task_id: str) -> int:
    return result.task(task_id).start_index


def test_milestone_closes_on_the_predecessor_finish_day() -> None:
    prog = program(
        [task("a", 3), task("m", 0), task("b", 2)],
        [dep("a", "m"), dep("m", "b")],
    )
    result = _accepted(plan(prog, FAST))
    assert result.task("a").finish == date(2026, 10, 7)
    assert result.task("m").finish == date(2026, 10, 7)
    assert result.task("m").start_index == result.task("m").end_index
    assert result.task("b").start == date(2026, 10, 8)


def test_fs_chain_dates() -> None:
    result = _accepted(plan(program([task("a", 3), task("b", 2)], [dep("a", "b")]), FAST))
    assert result.task("b").start == date(2026, 10, 8)
    assert result.task("b").finish == date(2026, 10, 9)
    assert result.outcome.claim is Claim.OPTIMAL


def test_shared_stand_serialises_two_projects() -> None:
    prog = program(
        [task("a", 3, "p1", demands=uses("st")), task("b", 3, "p2", demands=uses("st"))],
        resources=[stand()],
    )
    result = _accepted(plan(prog, FAST))
    assert sorted((_start(result, "a"), _start(result, "b"))) == [0, 3]


@pytest.mark.parametrize(
    ("kind", "lag", "expected_b_start"),
    [("SS", 2, 2), ("FF", 1, 4), ("FS", -2, 3), ("SF", 4, 2)],
)
def test_generalised_relations(kind: str, lag: int, expected_b_start: int) -> None:
    prog = program([task("a", 5), task("b", 2)], [dep("a", "b", kind, lag)])
    result = _accepted(plan(prog, FAST))
    assert _start(result, "b") == expected_b_start


def test_max_lag_is_respected() -> None:
    prog = program(
        [task("a", 3), task("b", 2, earliest_start=date(2026, 10, 12))],
        [dep("a", "b", "SS", 0, max_lag_wd=0)],
    )
    result = _accepted(plan(prog, FAST))
    assert _start(result, "a") == _start(result, "b") == 5


def test_impossible_deadline_is_proven_infeasible() -> None:
    prog = program([task("a", 5), task("b", 5, deadline=date(2026, 10, 9))], [dep("a", "b")])
    result = plan(prog, FAST)
    assert not result.outcome.ok
    assert result.outcome.claim is Claim.INFEASIBLE
    assert result.tasks == []


def test_done_and_in_progress_work() -> None:
    prog = program(
        [
            task(
                "a",
                4,
                status=TaskStatus.DONE,
                actual_start=date(2026, 9, 28),
                actual_finish=date(2026, 10, 1),
            ),
            task("b", 4, status=TaskStatus.IN_PROGRESS, actual_start=date(2026, 10, 1), remaining_wd=2),
            task("c", 1),
        ],
        [dep("a", "b"), dep("b", "c")],
    )
    result = _accepted(plan(prog, FAST))
    assert result.task("a").start == date(2026, 9, 28)
    assert result.task("b").start == date(2026, 10, 1)
    assert result.task("b").finish == date(2026, 10, 6)
    assert _start(result, "c") == 2


def test_pinned_task_keeps_its_dates() -> None:
    prog = program(
        [
            task("a", 3, demands=uses("st")),
            task(
                "b",
                3,
                "p2",
                demands=uses("st"),
                pinned=True,
                planned_start=date(2026, 10, 5),
                planned_finish=date(2026, 10, 7),
            ),
        ],
        resources=[stand()],
    )
    result = _accepted(plan(prog, FAST))
    assert result.task("b").start == date(2026, 10, 5)
    assert _start(result, "a") == 3


def test_vacation_delays_named_person() -> None:
    prog = program(
        [task("a", 2, demands=uses("ivanov", 10))],
        resources=[person("ivanov")],
        capacity_exceptions=[
            CapacityException(
                resource_id="ivanov",
                start=date(2026, 10, 5),
                end=date(2026, 10, 9),
                reason=ExceptionReason.VACATION,
            )
        ],
    )
    result = _accepted(plan(prog, FAST))
    assert result.task("a").start == date(2026, 10, 12)


def test_skill_pool_is_bound_to_people() -> None:
    prog = program(
        [task("a", 3, demands=needs("d")), task("b", 3, "p2", demands=needs("d"))],
        resources=[person("x", ["d"])],
        skills=["d"],
    )
    result = _accepted(plan(prog, FAST))
    assert sorted((_start(result, "a"), _start(result, "b"))) == [0, 3]
    assert result.task("a").bound == {"d": "x"}
    assert result.metadata["skill_binding"] == "exact"


def test_greedy_is_heuristic_and_checked() -> None:
    prog = program(
        [task("a", 3, demands=uses("st")), task("b", 3, "p2", demands=uses("st")), task("c", 2)],
        [dep("a", "c", "SS", 1)],
        resources=[stand()],
    )
    result = _accepted(plan(prog, SolveConfig(solver="greedy")))
    assert result.outcome.claim is Claim.HEURISTIC_FEASIBLE
    assert check_plan(prog, result.tasks) == [
        v for v in result.violations if v.code != "APPROXIMATED_RELATION"
    ]


def test_stability_never_moves_work_earlier_than_approved() -> None:
    prog = program(
        [
            task(
                "a", 3, demands=uses("st"), planned_start=date(2026, 10, 7), planned_finish=date(2026, 10, 9)
            ),
            task(
                "b",
                3,
                "p2",
                demands=uses("st"),
                planned_start=date(2026, 10, 7),
                planned_finish=date(2026, 10, 9),
            ),
        ],
        resources=[stand()],
    )
    result = _accepted(plan(prog, SolveConfig(time_limit_s=10, objective="stability")))
    assert all((row.shift_wd or 0) >= 0 for row in result.tasks)
    assert sorted(row.shift_wd for row in result.tasks) == [0, 3]


def test_unproven_infeasibility_is_not_claimed() -> None:
    cpsat = SolveConfig()
    assert _claim(SolverStatus.INFEASIBLE, False, cpsat, False, proof=True) is Claim.INFEASIBLE
    assert _claim(SolverStatus.INFEASIBLE, False, cpsat, False, proof=False) is Claim.ERROR
    greedy = SolveConfig(solver="greedy")
    assert _claim(SolverStatus.INFEASIBLE, False, greedy, False, proof=True) is Claim.ERROR


def test_same_seed_same_plan_hash() -> None:
    prog = program(
        [task(f"t{i}", 2 + i % 3, f"p{i % 2}", demands=uses("st")) for i in range(6)],
        [dep("t0", "t2"), dep("t1", "t3", "SS", 1)],
        resources=[stand()],
    )
    first = plan(prog, FAST)
    second = plan(prog, FAST)
    assert first.evidence["plan_hash"] == second.evidence["plan_hash"]
    assert first.evidence["input_hash"] == second.evidence["input_hash"]
