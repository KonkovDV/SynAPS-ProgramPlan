"""Conflict and data-quality analysis BEFORE optimisation (requirement F3).

Works on the source plan exactly as imported (planned / baseline dates):

* overloads of people, skill pools, stands and labs (exact daily profile);
* contention: overloads that involve two or more OKR projects;
* broken links: source dates that violate a dependency (typical after merging
  independent project files and adding cross-project links);
* deadline risk: CPM with all relation types and lags, no resources - negative
  float means the deadline is impossible whatever the resources; low float plus
  upstream overloads marks a milestone at risk.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from synaps_okrplan.calendar import WorkCalendar, WorkdayAxis
from synaps_okrplan.capacity import Load, overloads
from synaps_okrplan.compiler import daily_availability, program_calendar
from synaps_okrplan.cpm import cpm
from synaps_okrplan.model import (
    DependencyType,
    OKRProgram,
    ResourceKind,
    TaskStatus,
    difference_constraints,
)


class ConflictKind(StrEnum):
    OVERLOAD = "OVERLOAD"
    SHARED_CONTENTION = "SHARED_CONTENTION"
    SKILL_POOL_OVERLOAD = "SKILL_POOL_OVERLOAD"
    LINK_BROKEN = "LINK_BROKEN"
    DEADLINE_IMPOSSIBLE = "DEADLINE_IMPOSSIBLE"
    DEADLINE_AT_RISK = "DEADLINE_AT_RISK"
    DEADLINE_MISSED_IN_SOURCE = "DEADLINE_MISSED_IN_SOURCE"


@dataclass
class Conflict:
    kind: ConflictKind
    message: str
    tasks: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    resource_id: str | None = None
    start: date | None = None
    end: date | None = None
    peak_units: int | None = None
    available_units: int | None = None
    severity: str = "high"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "message": self.message,
            "tasks": self.tasks,
            "projects": self.projects,
            "resource_id": self.resource_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "peak_units": self.peak_units,
            "available_units": self.available_units,
            "severity": self.severity,
        }


@dataclass
class QualityIssue:
    code: str
    message: str
    tasks: list[str] = field(default_factory=list)
    severity: str = "warning"

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "tasks": self.tasks, "severity": self.severity}


@dataclass
class Analysis:
    conflicts: list[Conflict]
    quality: list[QualityIssue]
    cpm_finish: date | None
    critical_tasks: list[str]
    milestone_risk: dict[str, str]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for conflict in self.conflicts:
            counts[conflict.kind.value] += 1
        return dict(counts)


def source_positions(program: OKRProgram, axis: WorkdayAxis) -> dict[str, tuple[int, int]]:
    """Axis positions of active tasks according to the source plan."""
    refs = program.reference_dates()
    out: dict[str, tuple[int, int]] = {}
    for task in program.tasks:
        if task.status is TaskStatus.DONE or task.id not in refs:
            continue
        start, finish = refs[task.id]
        if task.duration_wd == 0:
            event = axis.boundary_after(finish)
            out[task.id] = (event, event)
        else:
            out[task.id] = (axis.index_on_or_after(start), axis.boundary_after(finish))
    return out


def analyze(program: OKRProgram) -> Analysis:
    calendar = program_calendar(program)
    axis = WorkdayAxis.build(calendar, program.program.planning_start, program.program.horizon_end)
    positions = source_positions(program, axis)
    conflicts: list[Conflict] = []
    conflicts.extend(_overloads(program, axis, positions))
    conflicts.extend(_broken_links(program, axis, positions))
    risk_conflicts, cpm_finish, critical, risk = _deadline_risk(program, axis, conflicts)
    conflicts.extend(risk_conflicts)
    return Analysis(
        conflicts=conflicts,
        quality=quality_issues(program, calendar, axis),
        cpm_finish=cpm_finish,
        critical_tasks=critical,
        milestone_risk=risk,
    )


def _overloads(
    program: OKRProgram, axis: WorkdayAxis, positions: dict[str, tuple[int, int]]
) -> list[Conflict]:
    from synaps_okrplan.calendar import DayCounter

    counter = DayCounter(program_calendar(program))
    base = counter.ordinal_on_or_after(program.program.planning_start)
    availability = daily_availability(program, axis, counter, base)
    tasks = {task.id: task for task in program.tasks}
    loads: dict[str, list[Load]] = defaultdict(list)
    skill_loads: dict[str, list[Load]] = defaultdict(list)
    for task_id, (start, end) in positions.items():
        for demand in tasks[task_id].demands:
            row = Load(start=start, end=end, units=demand.units, ref=task_id)
            if demand.resource_id is not None:
                loads[demand.resource_id].append(row)
            elif demand.skill_id is not None:
                skill_loads[demand.skill_id].append(row)
    out: list[Conflict] = []
    resources = {resource.id: resource for resource in program.resources}
    for resource_id, rows in loads.items():
        for excess in overloads(rows, availability[resource_id], 0):
            projects = sorted({tasks[ref].project_id for ref in excess.refs})
            resource = resources[resource_id]
            shared = len(projects) > 1
            kind = ConflictKind.SHARED_CONTENTION if shared else ConflictKind.OVERLOAD
            label = "стенд" if resource.kind in (ResourceKind.STAND, ResourceKind.LAB) else "ресурс"
            out.append(
                Conflict(
                    kind=kind,
                    message=(
                        f"{label} {resource.code}: нужно {excess.peak_units}, доступно {excess.available} "
                        f"({_span(axis, excess.start, excess.end)})"
                        + (f"; конкурируют ОКР: {', '.join(projects)}" if shared else "")
                    ),
                    tasks=list(excess.refs),
                    projects=projects,
                    resource_id=resource_id,
                    start=axis.start_date(excess.start),
                    end=axis.finish_date(excess.start, excess.end),
                    peak_units=excess.peak_units,
                    available_units=excess.available,
                )
            )
    for skill_id, rows in skill_loads.items():
        members = [r for r in program.resources if skill_id in r.skills]
        pool = [sum(availability[m.id][day] for m in members) for day in range(len(axis))]
        for excess in overloads(rows, pool, 0):
            projects = sorted({tasks[ref].project_id for ref in excess.refs})
            out.append(
                Conflict(
                    kind=ConflictKind.SKILL_POOL_OVERLOAD,
                    message=(
                        f"компетенция {skill_id}: нужно {excess.peak_units / 10:g} FTE, "
                        f"доступно {excess.available / 10:g} FTE ({_span(axis, excess.start, excess.end)})"
                    ),
                    tasks=list(excess.refs),
                    projects=projects,
                    resource_id=f"skill:{skill_id}",
                    start=axis.start_date(excess.start),
                    end=axis.finish_date(excess.start, excess.end),
                    peak_units=excess.peak_units,
                    available_units=excess.available,
                    severity="high" if len(projects) > 1 else "medium",
                )
            )
    return out


def _span(axis: WorkdayAxis, start: int, end: int) -> str:
    return (
        f"{axis.start_date(start):%d.%m.%Y}–{axis.finish_date(start, end):%d.%m.%Y}, {end - start} раб. дн."
    )


def _broken_links(
    program: OKRProgram, axis: WorkdayAxis, positions: dict[str, tuple[int, int]]
) -> list[Conflict]:
    tasks = {task.id: task for task in program.tasks}
    out: list[Conflict] = []
    for edge in program.dependencies:
        src = positions.get(edge.src_task_id)
        dst = positions.get(edge.dst_task_id)
        if src is None or dst is None or not edge.hard:
            continue
        src_value = src[1] if edge.type in (DependencyType.FS, DependencyType.FF) else src[0]
        dst_value = dst[1] if edge.type in (DependencyType.FF, DependencyType.SF) else dst[0]
        delta = dst_value - src_value
        if delta < edge.lag_wd or (edge.max_lag_wd is not None and delta > edge.max_lag_wd):
            projects = sorted({tasks[edge.src_task_id].project_id, tasks[edge.dst_task_id].project_id})
            out.append(
                Conflict(
                    kind=ConflictKind.LINK_BROKEN,
                    message=(
                        f"связь {edge.type.value} {edge.src_task_id} → {edge.dst_task_id} нарушена "
                        f"в исходных планах: зазор {delta} раб. дн., требуется ≥ {edge.lag_wd}"
                    ),
                    tasks=[edge.src_task_id, edge.dst_task_id],
                    projects=projects,
                    severity="high" if len(projects) > 1 else "medium",
                )
            )
    return out


def _deadline_risk(
    program: OKRProgram, axis: WorkdayAxis, conflicts: list[Conflict]
) -> tuple[list[Conflict], date | None, list[str], dict[str, str]]:
    tasks = {task.id: task for task in program.tasks if task.status is not TaskStatus.DONE}
    durations = {task_id: task.duration_wd for task_id, task in tasks.items()}
    lower = {task_id: (1 if task.duration_wd == 0 else 0) for task_id, task in tasks.items()}
    for task_id, task in tasks.items():
        if task.earliest_start is not None:
            lower[task_id] = max(lower[task_id], axis.index_on_or_after(task.earliest_start))
    constraints = [(s, d, w) for s, d, w in difference_constraints(program) if s in tasks and d in tasks]
    upper: dict[str, int] = {}
    projects = {project.id: project for project in program.projects}
    for task_id, task in tasks.items():
        bounds = [b for b in (task.hard_finish, projects[task.project_id].deadline) if b is not None]
        if bounds:
            upper[task_id] = axis.boundary_after(min(bounds))
    result = cpm(durations, constraints, lower, upper_end=upper)
    overloaded = {ref for conflict in conflicts if conflict.peak_units for ref in conflict.tasks}
    preds: dict[str, set[str]] = defaultdict(set)
    for src, dst, _ in constraints:
        preds[dst].add(src)
    out: list[Conflict] = []
    risk: dict[str, str] = {}
    refs = program.reference_dates()
    for task_id in sorted(upper):
        task = tasks[task_id]
        slack = result.total_float[task_id]
        upstream = _ancestors(task_id, preds)
        hot = sorted(upstream & overloaded)
        bound = min(b for b in (task.hard_finish, projects[task.project_id].deadline) if b is not None)
        if slack < 0:
            risk[task_id] = "impossible"
            out.append(
                Conflict(
                    kind=ConflictKind.DEADLINE_IMPOSSIBLE,
                    tasks=[task_id],
                    projects=[task.project_id],
                    message=f"срок {bound:%d.%m.%Y} для «{task.name}» недостижим даже без учёта "
                    f"ресурсов: не хватает {-slack} раб. дн. по критическому пути",
                )
            )
        elif hot and slack < 20:
            risk[task_id] = "high"
            out.append(
                Conflict(
                    kind=ConflictKind.DEADLINE_AT_RISK,
                    tasks=[task_id, *hot[:10]],
                    projects=[task.project_id],
                    severity="medium",
                    message=f"«{task.name}»: резерв {slack} раб. дн. до срока {bound:%d.%m.%Y}, "
                    f"а {len(hot)} предшествующих работ попадают в перегрузки",
                )
            )
        else:
            risk[task_id] = "low" if slack >= 20 else "medium"
        source = refs.get(task_id)
        if source is not None and source[1] > bound:
            out.append(
                Conflict(
                    kind=ConflictKind.DEADLINE_MISSED_IN_SOURCE,
                    tasks=[task_id],
                    projects=[task.project_id],
                    message=f"в исходном плане «{task.name}» завершается {source[1]:%d.%m.%Y}, "
                    f"позже срока {bound:%d.%m.%Y}",
                )
            )
    finish = axis.event_date(min(result.finish, len(axis))) if tasks else None
    critical = sorted(t for t in result.critical if durations[t] > 0)
    return out, finish, critical, risk


def _ancestors(node: str, preds: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(preds.get(node, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(preds.get(current, ()))
    return seen


def quality_issues(program: OKRProgram, calendar: WorkCalendar, axis: WorkdayAxis) -> list[QualityIssue]:
    """DCMA-14-style hygiene checks that make a consolidated plan untrustworthy."""
    out: list[QualityIssue] = []
    has_pred = {edge.dst_task_id for edge in program.dependencies}
    has_succ = {edge.src_task_id for edge in program.dependencies}
    dangling = [t.id for t in program.tasks if t.id not in has_pred and t.id not in has_succ]
    if dangling:
        out.append(QualityIssue("DANGLING", f"{len(dangling)} работ без связей", dangling[:50]))
    open_end = [
        t.id for t in program.tasks if t.id in has_pred and t.id not in has_succ and t.duration_wd > 0
    ]
    if open_end:
        out.append(
            QualityIssue(
                "OPEN_END", f"{len(open_end)} работ без последователей (не ведут к вехе)", open_end[:50]
            )
        )
    no_res = [t.id for t in program.tasks if t.duration_wd > 0 and not t.demands]
    if no_res:
        out.append(QualityIssue("NO_RESOURCES", f"{len(no_res)} работ без назначенных ресурсов", no_res[:50]))
    long_tasks = [t.id for t in program.tasks if t.duration_wd > 66]
    if long_tasks:
        out.append(
            QualityIssue(
                "LONG_TASK", f"{len(long_tasks)} работ длиннее 66 раб. дн. (~3 мес.)", long_tasks[:50]
            )
        )
    leads = [f"{e.src_task_id}->{e.dst_task_id}" for e in program.dependencies if e.lag_wd < 0]
    if leads:
        out.append(QualityIssue("NEGATIVE_LAG", f"{len(leads)} связей с отрицательным лагом", leads[:50]))
    hard = [t.id for t in program.tasks if t.hard_finish is not None or t.earliest_start is not None]
    if len(hard) > max(5, len(program.tasks) // 20):
        out.append(QualityIssue("HARD_CONSTRAINTS", f"{len(hard)} работ с жёсткими датами (>5%)", hard[:50]))
    start = program.program.planning_start
    stale = [
        t.id
        for t in program.tasks
        if t.status is TaskStatus.PLANNED and t.planned_start is not None and t.planned_start < start
    ]
    if stale:
        out.append(
            QualityIssue(
                "STALE_PLANNED", f"{len(stale)} работ должны были начаться до даты статуса", stale[:50]
            )
        )
    for task in program.tasks:
        for value in (task.planned_start, task.earliest_start):
            if value is not None and task.duration_wd > 0 and not calendar.is_workday(value):
                out.append(
                    QualityIssue(
                        "NONWORKING_DATE",
                        f"дата {value} работы {task.id} — нерабочий день",
                        [task.id],
                        "info",
                    )
                )
                break
    return out
