"""Roll a verified plan forward to a new status date and apply disruptions.

The previous plan becomes the baseline (stability is measured against what was
announced), work finished before the new status date becomes DONE, work in
flight becomes IN_PROGRESS with its remaining duration, and the freeze window
keeps the near-term part of the plan unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from synaps_okrplan.calendar import WorkdayAxis
from synaps_okrplan.compiler import program_calendar
from synaps_okrplan.model import (
    Baseline,
    BaselineDates,
    CapacityException,
    ExceptionReason,
    OKRProgram,
    TaskStatus,
)
from synaps_okrplan.result import PlanResult


@dataclass
class Disruption:
    delay_wd: dict[str, int] = field(default_factory=dict)
    unavailable: list[tuple[str, date, date]] = field(default_factory=list)
    capacity_change: dict[str, int] = field(default_factory=dict)


def roll_forward(
    program: OKRProgram,
    plan: PlanResult,
    status_date: date,
    *,
    freeze_wd: int = 0,
) -> OKRProgram:
    if not plan.outcome.ok:
        raise ValueError("only an accepted plan (outcome.ok) can be rolled forward")
    if status_date < program.program.planning_start:
        raise ValueError("new status date precedes the current one")
    calendar = program_calendar(program)
    axis = WorkdayAxis.build(calendar, status_date, program.program.horizon_end)
    rows = {row.task_id: row for row in plan.tasks}
    tasks = []
    for task in program.tasks:
        row = rows[task.id]
        update: dict[str, object] = {"planned_start": row.start, "planned_finish": row.finish}
        if task.status is not TaskStatus.DONE:
            if row.finish < status_date or (row.is_milestone and row.finish < status_date):
                update.update(
                    status=TaskStatus.DONE,
                    actual_start=row.start,
                    actual_finish=row.finish,
                    remaining_wd=None,
                )
            elif row.start < status_date and not row.is_milestone:
                remaining = calendar.count_workdays(status_date, row.finish)
                update.update(
                    status=TaskStatus.IN_PROGRESS,
                    actual_start=task.actual_start or row.start,
                    remaining_wd=remaining,
                )
        tasks.append(task.model_copy(update=update))
    freeze_until = None
    if freeze_wd > 0 and len(axis) > freeze_wd:
        freeze_until = axis.days[freeze_wd]
    baseline = Baseline(
        id=f"plan:{plan.scenario_id}:{plan.evidence.get('plan_hash', '')[:12]}",
        task_dates={row.task_id: BaselineDates(start=row.start, finish=row.finish) for row in plan.tasks},
    )
    data = program.model_copy(
        update={
            "tasks": tasks,
            "baseline": baseline,
            "freeze": program.freeze.model_copy(update={"freeze_until": freeze_until}),
            "program": program.program.model_copy(update={"status_date": status_date}),
        }
    ).model_dump()
    return OKRProgram.model_validate(data)


def apply_disruption(program: OKRProgram, disruption: Disruption) -> OKRProgram:
    tasks = []
    for task in program.tasks:
        extra = disruption.delay_wd.get(task.id, 0)
        if extra and task.status is TaskStatus.IN_PROGRESS:
            base = task.remaining_wd if task.remaining_wd is not None else task.duration_wd
            task = task.model_copy(
                update={"remaining_wd": base + extra, "duration_wd": task.duration_wd + extra}
            )
        elif extra and task.status is TaskStatus.PLANNED and task.duration_wd > 0:
            task = task.model_copy(update={"duration_wd": task.duration_wd + extra})
        tasks.append(task)
    exceptions = list(program.capacity_exceptions)
    for resource_id, start, end in disruption.unavailable:
        exceptions.append(
            CapacityException(resource_id=resource_id, start=start, end=end, reason=ExceptionReason.OTHER)
        )
    resources = [
        r.model_copy(
            update={"capacity_units": max(1, r.capacity_units + disruption.capacity_change.get(r.id, 0))}
        )
        for r in program.resources
    ]
    data = program.model_copy(
        update={"tasks": tasks, "capacity_exceptions": exceptions, "resources": resources}
    ).model_dump()
    return OKRProgram.model_validate(data)


def default_next_status(program: OKRProgram, weeks: int = 4) -> date:
    return program.program.planning_start + timedelta(weeks=weeks)
