from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
from synaps.model import ScheduleResult, SolverStatus
from synaps.validation import verify_schedule_result

from synaps_programplan.checker import check_plan
from synaps_programplan.compiler import compile_program
from synaps_programplan.model import OKRProgram, TaskStatus
from synaps_programplan.planner import SolveConfig, _warm_start, plan
from synaps_programplan.result import PlanResult, Severity, TaskPlan
from synaps_programplan.synthetic import SyntheticSpec, generate
from tests.conftest import dep, needs, person, program, stand, task, uses


def _hard_codes(prog: OKRProgram, rows: list[TaskPlan]) -> set[str]:
    return {v.code for v in check_plan(prog, rows) if v.severity is Severity.HARD}


@pytest.fixture(scope="module")
def accepted() -> tuple[OKRProgram, PlanResult]:
    prog = generate(SyntheticSpec(projects=3, tasks_per_stage=2, seed=11))
    result = plan(prog, SolveConfig(time_limit_s=10))
    assert result.outcome.ok
    return prog, result


def _moved(prog: OKRProgram, row: TaskPlan, delta: int) -> TaskPlan:
    axis = compile_program(prog).axis
    start, end = row.start_index + delta, row.end_index + delta
    if row.is_milestone:
        day = axis.event_date(end)
        return row.model_copy(update={"start": day, "finish": day, "start_index": start, "end_index": end})
    return row.model_copy(
        update={
            "start": axis.start_date(start),
            "finish": axis.finish_date(start, end),
            "start_index": start,
            "end_index": end,
        }
    )


def _kernel_feasible(prog: OKRProgram, rows: list[TaskPlan]) -> bool:
    compiled = compile_program(prog)
    hint = {row.task_id: (row.start_index, row.end_index) for row in rows if row.task_id in compiled.windows}
    result = ScheduleResult(
        solver_name="oracle", status=SolverStatus.FEASIBLE, assignments=_warm_start(compiled, hint)
    )
    return verify_schedule_result(compiled.problem, result).feasible


def test_accepted_plan_is_clean(accepted: tuple[OKRProgram, PlanResult]) -> None:
    prog, result = accepted
    assert _hard_codes(prog, result.tasks) == set()
    assert _kernel_feasible(prog, result.tasks)


def test_no_false_accepts_under_random_shifts(accepted: tuple[OKRProgram, PlanResult]) -> None:
    prog, result = accepted
    rng = random.Random(2026)
    movable = [r for r in result.tasks if r.status is TaskStatus.PLANNED]
    false_accepts, rejected = [], 0
    for _ in range(150):
        rows = list(result.tasks)
        for _ in range(rng.randint(1, 3)):
            pick = rng.choice(movable).task_id
            index = next(i for i, r in enumerate(rows) if r.task_id == pick)
            delta = rng.choice([-5, -3, -2, -1, 1, 2, 4, 8])
            if rows[index].start_index + delta < (1 if rows[index].is_milestone else 0):
                continue
            rows[index] = _moved(prog, rows[index], delta)
        domain_hard = _hard_codes(prog, rows)
        rejected += bool(domain_hard)
        if not domain_hard and not _kernel_feasible(prog, rows):
            false_accepts.append([r.task_id for r in rows if r not in result.tasks])
    assert false_accepts == []
    assert rejected > 0


def _base() -> tuple[OKRProgram, PlanResult]:
    prog = program(
        [
            task("a", 3, demands=uses("st")),
            task("b", 2, "p2", demands=uses("st"), deadline=date(2026, 10, 30)),
            task("c", 2, demands=needs("d")),
            task("m", 0),
            task(
                "done",
                2,
                status=TaskStatus.DONE,
                actual_start=date(2026, 9, 28),
                actual_finish=date(2026, 9, 29),
            ),
            task("pin", 1, pinned=True, planned_start=date(2026, 10, 14), planned_finish=date(2026, 10, 14)),
        ],
        [dep("a", "m"), dep("done", "c")],
        resources=[stand(), person("x", ["d"])],
        skills=["d"],
    )
    result = plan(prog, SolveConfig(time_limit_s=10))
    assert result.outcome.ok
    return prog, result


def _replace(rows: list[TaskPlan], task_id: str, **update: object) -> list[TaskPlan]:
    return [r.model_copy(update=update) if r.task_id == task_id else r for r in rows]


@pytest.mark.parametrize(
    ("task_id", "update", "code"),
    [
        ("a", {"finish": date(2026, 10, 12)}, "DURATION_MISMATCH"),
        ("a", {"start": date(2026, 10, 3)}, "CALENDAR_BROKEN"),
        ("done", {"start": date(2026, 9, 30), "finish": date(2026, 10, 1)}, "DONE_MOVED"),
        ("pin", {"start": date(2026, 10, 15), "finish": date(2026, 10, 15)}, "PINNED_MOVED"),
        ("b", {"start": date(2026, 11, 2), "finish": date(2026, 11, 3)}, "DEADLINE_MISSED"),
        ("m", {"start": date(2026, 10, 5), "finish": date(2026, 10, 5)}, "PRECEDENCE_FS_BROKEN"),
    ],
)
def test_each_violation_is_detected(task_id: str, update: dict[str, object], code: str) -> None:
    prog, result = _base()
    assert code in _hard_codes(prog, _replace(result.tasks, task_id, **update))


def test_resource_and_skill_overloads_are_detected() -> None:
    prog, result = _base()
    a = result.task("a")
    collide = _replace(result.tasks, "b", start=a.start, finish=a.start + timedelta(days=1))
    assert "CAPACITY_EXCEEDED" in _hard_codes(prog, collide)
    extra = task("c2", 2, demands=needs("d"))
    prog2 = OKRProgram.model_validate(
        {**prog.model_dump(), "tasks": [*prog.model_dump()["tasks"], extra.model_dump()]}
    )
    c = result.task("c")
    row = c.model_copy(update={"task_id": "c2", "name": "c2", "bound": {}})
    assert "SKILL_POOL_EXCEEDED" in _hard_codes(prog2, [*result.tasks, row])


def test_missing_and_unknown_tasks() -> None:
    prog, result = _base()
    assert "MISSING_TASK" in _hard_codes(prog, result.tasks[1:])
    ghost = result.tasks[0].model_copy(update={"task_id": "ghost"})
    assert "UNKNOWN_TASK" in _hard_codes(prog, [*result.tasks, ghost])
