"""Independent domain checker.

Judges a dated plan against the ORIGINAL ``OKRProgram`` - not against the
compiled kernel problem - and imports neither the compiler nor any solver.
Every date is re-derived from the calendars, so a decoding bug in the adapter
shows up here as a violation instead of being certified.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from synaps_programplan.calendar import DayCounter, WorkCalendar
from synaps_programplan.capacity import Load, overloads
from synaps_programplan.model import (
    DependencyType,
    OKRProgram,
    Task,
    TaskStatus,
    skill_pool_conflicts,
)
from synaps_programplan.result import Severity, TaskPlan, Violation

HARD = Severity.HARD


class _Ctx:
    def __init__(self, program: OKRProgram) -> None:
        self.program = program
        cal = next(c for c in program.calendars if c.id == program.program.calendar_id)
        self.calendar = WorkCalendar.from_model(cal)
        self.counter = DayCounter(self.calendar)
        self.tasks = {task.id: task for task in program.tasks}
        self.projects = {project.id: project for project in program.projects}
        self.planning_start = program.program.planning_start
        self.base = self.counter.ordinal_on_or_after(self.planning_start)
        self.refs = program.reference_dates()

    def start_anchor(self, task: Task, row: TaskPlan) -> int:
        if task.duration_wd == 0:
            return self.counter.boundary_after(row.finish)
        return self.counter.ordinal_on_or_after(row.start)

    def end_anchor(self, row: TaskPlan) -> int:
        return self.counter.boundary_after(row.finish)


def check_plan(program: OKRProgram, plan: list[TaskPlan]) -> list[Violation]:
    ctx = _Ctx(program)
    violations: list[Violation] = []
    rows = _identity(ctx, plan, violations)
    for task_id, row in rows.items():
        _check_task(ctx, ctx.tasks[task_id], row, violations)
    _check_dependencies(ctx, rows, violations)
    _check_capacity(ctx, rows, violations)
    _check_skills(ctx, rows, violations)
    return violations


def _identity(ctx: _Ctx, plan: list[TaskPlan], out: list[Violation]) -> dict[str, TaskPlan]:
    rows: dict[str, TaskPlan] = {}
    for row in plan:
        if row.task_id not in ctx.tasks:
            out.append(
                Violation(
                    code="UNKNOWN_TASK",
                    severity=HARD,
                    message=f"plan has unknown task {row.task_id}",
                    task_ids=[row.task_id],
                )
            )
            continue
        if row.task_id in rows:
            out.append(
                Violation(
                    code="DUPLICATE_TASK",
                    severity=HARD,
                    message=f"task {row.task_id} planned twice",
                    task_ids=[row.task_id],
                )
            )
        rows[row.task_id] = row
    for task_id in ctx.tasks:
        if task_id not in rows:
            out.append(
                Violation(
                    code="MISSING_TASK",
                    severity=HARD,
                    message=f"task {task_id} is not planned",
                    task_ids=[task_id],
                )
            )
    return rows


def _check_task(ctx: _Ctx, task: Task, row: TaskPlan, out: list[Violation]) -> None:
    ids = [task.id]
    if task.status is TaskStatus.DONE:
        if row.start != task.actual_start or row.finish != task.actual_finish:
            out.append(
                Violation(
                    code="DONE_MOVED",
                    severity=HARD,
                    message=f"completed task {task.id} must keep its actual dates",
                    task_ids=ids,
                )
            )
        return
    if task.status is TaskStatus.IN_PROGRESS and row.start != task.actual_start:
        out.append(
            Violation(
                code="STARTED_MOVED",
                severity=HARD,
                message=f"started task {task.id} must keep actual_start",
                task_ids=ids,
            )
        )
    if task.status is TaskStatus.IN_PROGRESS and row.finish < ctx.planning_start:
        out.append(
            Violation(
                code="FORECAST_BEFORE_STATUS",
                severity=HARD,
                message=f"task {task.id} forecast finish {row.finish} is before the status date",
                task_ids=ids,
            )
        )
    if task.duration_wd > 0 and not ctx.calendar.is_workday(row.start):
        out.append(
            Violation(
                code="CALENDAR_BROKEN",
                severity=HARD,
                message=f"task {task.id} starts on a non-working day {row.start}",
                task_ids=ids,
            )
        )
    if not ctx.calendar.is_workday(row.finish):
        out.append(
            Violation(
                code="CALENDAR_BROKEN",
                severity=HARD,
                message=f"task {task.id} finishes on a non-working day {row.finish}",
                task_ids=ids,
            )
        )
    end = ctx.end_anchor(row)
    if task.status is TaskStatus.IN_PROGRESS:
        if task.remaining_wd is not None and end - ctx.base != task.remaining_wd:
            out.append(
                Violation(
                    code="DURATION_MISMATCH",
                    severity=HARD,
                    message=f"task {task.id} remaining work is {end - ctx.base} wd, "
                    f"expected {task.remaining_wd}",
                    task_ids=ids,
                )
            )
    else:
        start = ctx.start_anchor(task, row)
        if end - start != task.duration_wd:
            out.append(
                Violation(
                    code="DURATION_MISMATCH",
                    severity=HARD,
                    message=f"task {task.id} spans {end - start} wd, expected {task.duration_wd}",
                    task_ids=ids,
                )
            )
        earliest_ok = start >= ctx.base + (1 if task.duration_wd == 0 else 0)
        if not earliest_ok or row.start < ctx.planning_start:
            out.append(
                Violation(
                    code="HORIZON_BROKEN",
                    severity=HARD,
                    message=f"task {task.id} starts before the status date",
                    task_ids=ids,
                )
            )
        if task.earliest_start is not None and _start_date(task, row) < task.earliest_start:
            out.append(
                Violation(
                    code="WINDOW_BROKEN",
                    severity=HARD,
                    message=f"task {task.id} starts before earliest_start {task.earliest_start}",
                    task_ids=ids,
                )
            )
        _check_reference(ctx, task, row, start, out)
    if row.finish > ctx.program.program.horizon_end:
        out.append(
            Violation(
                code="HORIZON_BROKEN",
                severity=HARD,
                message=f"task {task.id} finishes after the program horizon",
                task_ids=ids,
            )
        )
    project = ctx.projects[task.project_id]
    for label, bound in (("deadline", task.hard_finish), ("project deadline", project.deadline)):
        if bound is not None and row.finish > bound:
            out.append(
                Violation(
                    code="DEADLINE_MISSED",
                    severity=HARD,
                    message=f"task {task.id} finishes {row.finish} after its {label} {bound}",
                    task_ids=ids,
                    details={"bound": bound.isoformat()},
                )
            )
    if task.due_date is not None and row.finish > task.due_date:
        late = ctx.counter.boundary_after(row.finish) - ctx.counter.boundary_after(task.due_date)
        out.append(
            Violation(
                code="DUE_MISSED",
                severity=Severity.KPI,
                message=f"task {task.id} is {late} wd past its due date {task.due_date}",
                task_ids=ids,
                details={"late_wd": late},
            )
        )


def _start_date(task: Task, row: TaskPlan) -> date:
    return row.finish if task.duration_wd == 0 else row.start


def _check_reference(ctx: _Ctx, task: Task, row: TaskPlan, start: int, out: list[Violation]) -> None:
    reference = ctx.refs.get(task.id) if _needs_reference(ctx, task) else None
    if reference is None:
        return
    ref_start, ref_finish = reference
    ids = [task.id]
    frozen = ctx.program.freeze.freeze_until is not None and ref_start < ctx.program.freeze.freeze_until
    if (task.pinned and ctx.program.freeze.freeze_pinned) or frozen:
        moved = row.finish != ref_finish if task.duration_wd == 0 else row.start != ref_start
        if moved:
            code = "PINNED_MOVED" if task.pinned else "FROZEN_MOVED"
            out.append(
                Violation(
                    code=code,
                    severity=HARD,
                    message=f"task {task.id} moved from its fixed date",
                    task_ids=ids,
                )
            )
        return
    if task.shift_limit_wd is not None:
        ref = (
            ctx.counter.boundary_after(ref_finish)
            if task.duration_wd == 0
            else ctx.counter.ordinal_on_or_after(ref_start)
        )
        if abs(start - ref) > task.shift_limit_wd:
            out.append(
                Violation(
                    code="SHIFT_LIMIT_EXCEEDED",
                    severity=HARD,
                    message=f"task {task.id} moved {start - ref} wd (limit {task.shift_limit_wd})",
                    task_ids=ids,
                    details={"shift_wd": start - ref},
                )
            )


def _needs_reference(ctx: _Ctx, task: Task) -> bool:
    return task.pinned or task.shift_limit_wd is not None or ctx.program.freeze.freeze_until is not None


def _check_dependencies(ctx: _Ctx, rows: dict[str, TaskPlan], out: list[Violation]) -> None:
    for edge in ctx.program.dependencies:
        src_row = rows.get(edge.src_task_id)
        dst_row = rows.get(edge.dst_task_id)
        if src_row is None or dst_row is None:
            continue
        src, dst = ctx.tasks[edge.src_task_id], ctx.tasks[edge.dst_task_id]
        if dst.status is TaskStatus.DONE:
            continue
        dst_end = edge.type in (DependencyType.FF, DependencyType.SF)
        if dst.status is TaskStatus.IN_PROGRESS and not dst_end:
            continue
        src_value = (
            ctx.end_anchor(src_row)
            if edge.type in (DependencyType.FS, DependencyType.FF)
            else ctx.start_anchor(src, src_row)
        )
        dst_value = ctx.end_anchor(dst_row) if dst_end else ctx.start_anchor(dst, dst_row)
        delta = dst_value - src_value
        severity = HARD if edge.hard else Severity.WARNING
        ids = [edge.src_task_id, edge.dst_task_id]
        if delta < edge.lag_wd:
            out.append(
                Violation(
                    code=f"PRECEDENCE_{edge.type.value}_BROKEN",
                    severity=severity,
                    message=f"{edge.type.value} {edge.src_task_id} -> {edge.dst_task_id}: "
                    f"gap {delta} wd < lag {edge.lag_wd}",
                    task_ids=ids,
                )
            )
        if edge.max_lag_wd is not None and delta > edge.max_lag_wd:
            out.append(
                Violation(
                    code="MAX_LAG_BROKEN",
                    severity=severity,
                    message=f"{edge.type.value} {edge.src_task_id} -> {edge.dst_task_id}: "
                    f"gap {delta} wd > max lag {edge.max_lag_wd}",
                    task_ids=ids,
                )
            )


def _occupancy(ctx: _Ctx, task: Task, row: TaskPlan) -> tuple[int, int] | None:
    if task.status is TaskStatus.DONE or task.duration_wd == 0:
        return None
    start = ctx.base if task.status is TaskStatus.IN_PROGRESS else ctx.counter.ordinal_on_or_after(row.start)
    end = ctx.end_anchor(row)
    return (start, end) if end > start else None


def _check_capacity(ctx: _Ctx, rows: dict[str, TaskPlan], out: list[Violation]) -> None:
    loads: dict[str, list[Load]] = defaultdict(list)
    hi = ctx.base
    for task_id, row in rows.items():
        task = ctx.tasks[task_id]
        span = _occupancy(ctx, task, row)
        if span is None:
            continue
        hi = max(hi, span[1])
        for demand in task.demands:
            resource_id = demand.resource_id or row.bound.get(demand.skill_id or "")
            if resource_id is None:
                continue
            loads[resource_id].append(Load(start=span[0], end=span[1], units=demand.units, ref=task_id))
    resources = {resource.id: resource for resource in ctx.program.resources}
    for resource_id, rows_for in loads.items():
        resource = resources.get(resource_id)
        if resource is None:
            continue
        available = _availability(ctx, resource_id, ctx.base, hi)
        for excess in overloads(rows_for, available, ctx.base):
            out.append(
                Violation(
                    code="CAPACITY_EXCEEDED",
                    severity=HARD,
                    message=f"{resource.code}: {excess.peak_units} units used, "
                    f"{excess.available} available, {excess.end - excess.start} wd",
                    task_ids=list(excess.refs),
                    resource_id=resource_id,
                    details={"start_ordinal": excess.start, "end_ordinal": excess.end},
                )
            )


def _availability(ctx: _Ctx, resource_id: str, lo: int, hi: int) -> list[int]:
    resource = next(r for r in ctx.program.resources if r.id == resource_id)
    own: WorkCalendar | None = None
    if resource.calendar_id is not None and resource.calendar_id != ctx.program.program.calendar_id:
        row = next(c for c in ctx.program.calendars if c.id == resource.calendar_id)
        own = WorkCalendar.from_model(row)
    exceptions = [row for row in ctx.program.capacity_exceptions if row.resource_id == resource_id]
    out: list[int] = []
    for ordinal in range(lo, hi):
        day = ctx.counter.date_of(ordinal)
        units = resource.capacity_units
        if own is not None and not own.is_workday(day):
            units = 0
        for row_exc in exceptions:
            if row_exc.start <= day <= row_exc.end:
                units = min(units, row_exc.units_available)
        out.append(units)
    return out


def _check_skills(ctx: _Ctx, rows: dict[str, TaskPlan], out: list[Violation]) -> None:
    resources = {resource.id: resource for resource in ctx.program.resources}
    pooled = not skill_pool_conflicts(ctx.program)
    if pooled:
        _check_skill_pools(ctx, rows, out)
    advisory: list[str] = []
    for task_id, row in rows.items():
        task = ctx.tasks[task_id]
        if task.status is TaskStatus.DONE or task.duration_wd == 0:
            continue
        for demand in task.demands:
            if demand.skill_id is None:
                continue
            bound = row.bound.get(demand.skill_id)
            if bound is None and pooled:
                advisory.append(task_id)
                continue
            if bound is None:
                out.append(
                    Violation(
                        code="SKILL_UNBOUND",
                        severity=HARD,
                        message=f"task {task_id} skill {demand.skill_id} has no named person",
                        task_ids=[task_id],
                    )
                )
                continue
            person = resources.get(bound)
            if person is None or demand.skill_id not in person.skills or demand.units > person.capacity_units:
                out.append(
                    Violation(
                        code="SKILL_MISMATCH",
                        severity=HARD,
                        message=f"task {task_id}: {bound} cannot cover skill {demand.skill_id}",
                        task_ids=[task_id],
                        resource_id=bound,
                    )
                )
    if advisory:
        out.append(
            Violation(
                code="SKILL_BINDING_ADVISORY",
                severity=Severity.WARNING,
                message=f"{len(advisory)} skill demands are planned on department capacity without a named "
                "person (no clash-free naming exists for these dates)",
                task_ids=sorted(set(advisory))[:50],
            )
        )


def _check_skill_pools(ctx: _Ctx, rows: dict[str, TaskPlan], out: list[Violation]) -> None:
    """Exact pools: all demands of a skill fit the summed daily availability of its members."""
    members: dict[str, list[str]] = defaultdict(list)
    for resource in ctx.program.resources:
        for skill in resource.skills:
            members[skill].append(resource.id)
    loads: dict[str, list[Load]] = defaultdict(list)
    hi = ctx.base
    for task_id, row in rows.items():
        task = ctx.tasks[task_id]
        span = _occupancy(ctx, task, row)
        if span is None:
            continue
        hi = max(hi, span[1])
        for demand in task.demands:
            if demand.skill_id is not None:
                loads[demand.skill_id].append(
                    Load(start=span[0], end=span[1], units=demand.units, ref=task_id)
                )
    for skill_id, skill_loads in loads.items():
        profiles = [_availability(ctx, rid, ctx.base, hi) for rid in members.get(skill_id, [])]
        available = [sum(day) for day in zip(*profiles, strict=True)] if profiles else [0] * (hi - ctx.base)
        for excess in overloads(skill_loads, available, ctx.base):
            out.append(
                Violation(
                    code="SKILL_POOL_EXCEEDED",
                    severity=HARD,
                    message=f"skill {skill_id}: {excess.peak_units} units used, "
                    f"{excess.available} available, {excess.end - excess.start} wd",
                    task_ids=list(excess.refs),
                    resource_id=f"skill:{skill_id}",
                    details={"start_ordinal": excess.start, "end_ordinal": excess.end},
                )
            )
