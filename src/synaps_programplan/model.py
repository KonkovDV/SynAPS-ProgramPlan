"""Domain model of a consolidated R&D (OKR) program.

Time is measured in WORKING DAYS of the program calendar. Dates are calendar
``date`` values: a task with start ``S`` and duration ``d > 0`` occupies the
``d`` working days beginning at ``S``; its finish date is the last of them
(MS Project convention). A milestone (``d = 0``) is an event at the END of its
date.

Resource demand is in integer units. For a person, 10 units = 100% FTE, so
``capacity_units=10`` and a 50% assignment is ``units=5``.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

FTE_UNITS = 10


def _limit(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalendarBase(StrEnum):
    RU_PRODUCTION = "RU_PRODUCTION"
    FIVE_DAY = "FIVE_DAY"
    SEVEN_DAY = "SEVEN_DAY"


class Calendar(_Strict):
    id: str
    base: CalendarBase = CalendarBase.RU_PRODUCTION
    extra_holidays: list[date] = Field(default_factory=list)
    extra_workdays: list[date] = Field(default_factory=list)


class Program(_Strict):
    id: str
    name: str
    calendar_id: str
    horizon_start: date
    horizon_end: date
    # "Today" of the plan: nothing new may start before it.
    status_date: date | None = None
    control_dates: list[date] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.horizon_end <= self.horizon_start:
            raise ValueError("program.horizon_end must be after horizon_start")
        if self.status_date is not None and not (self.horizon_start <= self.status_date <= self.horizon_end):
            raise ValueError("program.status_date must lie inside the horizon")
        return self

    @property
    def planning_start(self) -> date:
        return self.status_date or self.horizon_start


class Project(_Strict):
    id: str
    code: str
    name: str
    priority: int = Field(default=500, ge=1, le=1000)
    customer: str | None = None
    # Executing plant. Load and dates are rolled up by this name.
    enterprise: str | None = None
    due_date: date | None = None
    deadline: date | None = None
    domain_attributes: dict[str, Any] = Field(default_factory=dict)


class WBSKind(StrEnum):
    STAGE = "STAGE"
    PHASE = "PHASE"
    PACKAGE = "PACKAGE"


class WBSNode(_Strict):
    id: str
    project_id: str
    parent_id: str | None = None
    code: str
    name: str
    kind: WBSKind = WBSKind.PACKAGE


class TaskKind(StrEnum):
    WORK = "WORK"
    MILESTONE = "MILESTONE"
    TEST = "TEST"
    REVIEW = "REVIEW"


class TaskStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class Demand(_Strict):
    resource_id: str | None = None
    skill_id: str | None = None
    units: int = Field(ge=1)

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.resource_id is None) == (self.skill_id is None):
            raise ValueError("demand needs exactly one of resource_id / skill_id")
        return self


class Task(_Strict):
    id: str
    project_id: str
    wbs_id: str | None = None
    name: str
    duration_wd: int = Field(ge=0)
    kind: TaskKind = TaskKind.WORK
    demands: list[Demand] = Field(default_factory=list)
    earliest_start: date | None = None
    latest_finish: date | None = None
    due_date: date | None = None
    deadline: date | None = None
    shift_limit_wd: int | None = Field(default=None, ge=0)
    pinned: bool = False
    status: TaskStatus = TaskStatus.PLANNED
    remaining_wd: int | None = Field(default=None, ge=0)
    actual_start: date | None = None
    actual_finish: date | None = None
    # Dates as they stand in the source plan (MS Project / Primavera / 1C).
    planned_start: date | None = None
    planned_finish: date | None = None
    baseline_start: date | None = None
    baseline_finish: date | None = None
    okr_stage: str | None = None
    deliverable: str | None = None
    acceptance_doc: str | None = None
    domain_attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind is TaskKind.MILESTONE and self.duration_wd != 0:
            raise ValueError(f"milestone {self.id} must have duration_wd = 0")
        if self.kind is TaskKind.MILESTONE and self.demands:
            raise ValueError(f"milestone {self.id} cannot demand resources")
        if self.status is TaskStatus.DONE and (self.actual_start is None or self.actual_finish is None):
            raise ValueError(f"DONE task {self.id} needs actual_start and actual_finish")
        if self.status is TaskStatus.IN_PROGRESS and self.actual_start is None:
            raise ValueError(f"IN_PROGRESS task {self.id} needs actual_start")
        return self

    @property
    def is_milestone(self) -> bool:
        return self.duration_wd == 0

    @property
    def hard_finish(self) -> date | None:
        """Tightest hard finish bound (deadline and latest_finish are both hard)."""
        bounds = [value for value in (self.deadline, self.latest_finish) if value is not None]
        return min(bounds) if bounds else None


class DependencyType(StrEnum):
    FS = "FS"
    SS = "SS"
    FF = "FF"
    SF = "SF"


class DependencySource(StrEnum):
    IMPORTED = "IMPORTED"
    MANUAL = "MANUAL"
    CROSS_PROJECT = "CROSS_PROJECT"


class Dependency(_Strict):
    src_task_id: str
    dst_task_id: str
    type: DependencyType = DependencyType.FS
    lag_wd: int = 0
    max_lag_wd: int | None = None
    hard: bool = True
    source: DependencySource = DependencySource.IMPORTED

    @model_validator(mode="after")
    def _lags(self) -> Self:
        if self.max_lag_wd is not None and self.max_lag_wd < self.lag_wd:
            raise ValueError("dependency max_lag_wd must be >= lag_wd")
        return self


class ResourceKind(StrEnum):
    PERSON = "PERSON"
    GROUP = "GROUP"
    STAND = "STAND"
    LAB = "LAB"
    EQUIPMENT = "EQUIPMENT"
    SOFTWARE = "SOFTWARE"


class Resource(_Strict):
    id: str
    kind: ResourceKind
    code: str
    name: str
    capacity_units: int = Field(ge=1)
    calendar_id: str | None = None
    skills: list[str] = Field(default_factory=list)
    org_unit: str | None = None


class Skill(_Strict):
    id: str
    code: str
    name: str


class ExceptionReason(StrEnum):
    VACATION = "VACATION"
    MAINTENANCE = "MAINTENANCE"
    BOOKED_EXTERNAL = "BOOKED_EXTERNAL"
    OTHER = "OTHER"


class CapacityException(_Strict):
    """Reduced availability on the inclusive date range ``[start, end]``."""

    resource_id: str
    start: date
    end: date
    units_available: int = Field(default=0, ge=0)
    reason: ExceptionReason = ExceptionReason.OTHER

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("capacity exception end must not precede start")
        return self


class BaselineDates(_Strict):
    start: date
    finish: date


class Baseline(_Strict):
    id: str
    approved_at: datetime | None = None
    approved_by: str | None = None
    task_dates: dict[str, BaselineDates] = Field(default_factory=dict)


class FreezePolicy(_Strict):
    # Tasks whose reference start falls before this date keep their dates.
    freeze_until: date | None = None
    freeze_pinned: bool = True


class RiskDriver(_Strict):
    """A named uncertainty (AACE 57R-09 risk driver): with ``probability`` it
    multiplies the duration of ``task_ids`` by a triangular factor."""

    id: str
    name: str
    probability: float = Field(gt=0, le=1)
    low: float = Field(default=1.0, gt=0)
    mode: float = Field(default=1.2, gt=0)
    high: float = Field(default=1.5, gt=0)
    task_ids: list[str] = Field(min_length=1)
    owner: str = ""
    # Drivers with the same group share one occurrence draw (comonotonic).
    group: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.low <= self.mode <= self.high:
            raise ValueError(f"risk driver {self.id}: need low <= mode <= high")
        return self


class ProvenanceKind(StrEnum):
    SYNTHETIC = "synthetic"
    OPEN_DATA = "open_data"
    CUSTOMER_ANONYMIZED = "customer_anonymized"
    EXPERIMENT = "experiment"


class Provenance(_Strict):
    kind: ProvenanceKind = ProvenanceKind.SYNTHETIC
    source: str = ""
    source_file_hash: str | None = None
    imported_at: datetime | None = None


class OKRProgram(_Strict):
    """Complete, self-contained input of one planning run."""

    schema_version: str = "SynAPS-ProgramPlan.program.v1"
    program: Program
    calendars: list[Calendar]
    projects: list[Project]
    wbs: list[WBSNode] = Field(default_factory=list)
    tasks: list[Task]
    dependencies: list[Dependency] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    capacity_exceptions: list[CapacityException] = Field(default_factory=list)
    baseline: Baseline | None = None
    freeze: FreezePolicy = Field(default_factory=FreezePolicy)
    risk_drivers: list[RiskDriver] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def _references(self) -> Self:
        max_tasks = _limit("SYNAPS_PROGRAMPLAN_MAX_TASKS", 100_000)
        max_links = _limit("SYNAPS_PROGRAMPLAN_MAX_LINKS", 400_000)
        max_resources = _limit("SYNAPS_PROGRAMPLAN_MAX_RESOURCES", 20_000)
        max_horizon = _limit("SYNAPS_PROGRAMPLAN_MAX_HORIZON_DAYS", 8_000)
        max_exclusions = _limit("SYNAPS_PROGRAMPLAN_MAX_EXCLUSIONS", 100_000)
        max_risks = _limit("SYNAPS_PROGRAMPLAN_MAX_RISK_DRIVERS", 10_000)
        if len(self.tasks) > max_tasks:
            raise ValueError(f"program has {len(self.tasks)} tasks; the limit is {max_tasks}")
        if len(self.dependencies) > max_links:
            raise ValueError(f"program has {len(self.dependencies)} links; the limit is {max_links}")
        if len(self.resources) > max_resources:
            raise ValueError(f"program has {len(self.resources)} resources; the limit is {max_resources}")
        if len(self.capacity_exceptions) > max_exclusions:
            count = len(self.capacity_exceptions)
            raise ValueError(f"program has {count} capacity exceptions; the limit is {max_exclusions}")
        if len(self.risk_drivers) > max_risks:
            raise ValueError(f"program has {len(self.risk_drivers)} risk drivers; the limit is {max_risks}")
        span = (self.program.horizon_end - self.program.horizon_start).days
        if span > max_horizon:
            raise ValueError(f"program horizon is {span} days; the limit is {max_horizon}")
        if self.program.horizon_start < date(2000, 1, 3):
            raise ValueError("program horizon starts before 2000-01-03, which the calendar does not support")
        issues: list[str] = []
        _unique(issues, "calendar", [row.id for row in self.calendars])
        _unique(issues, "project", [row.id for row in self.projects])
        _unique(issues, "wbs", [row.id for row in self.wbs])
        _unique(issues, "task", [row.id for row in self.tasks])
        _unique(issues, "resource", [row.id for row in self.resources])
        _unique(issues, "skill", [row.id for row in self.skills])
        calendars = {row.id for row in self.calendars}
        projects = {row.id for row in self.projects}
        wbs = {row.id: row for row in self.wbs}
        tasks = {row.id: row for row in self.tasks}
        resources = {row.id: row for row in self.resources}
        skills = {row.id for row in self.skills}
        if self.program.calendar_id not in calendars:
            issues.append(f"program calendar {self.program.calendar_id!r} is unknown")
        for node in self.wbs:
            if node.project_id not in projects:
                issues.append(f"wbs {node.id} references unknown project {node.project_id}")
            if node.parent_id is not None and node.parent_id not in wbs:
                issues.append(f"wbs {node.id} references unknown parent {node.parent_id}")
        for resource in self.resources:
            if resource.calendar_id is not None and resource.calendar_id not in calendars:
                issues.append(f"resource {resource.id} references unknown calendar")
            for skill in resource.skills:
                if skill not in skills:
                    issues.append(f"resource {resource.id} references unknown skill {skill}")
        for task in self.tasks:
            _task_refs(issues, task, projects, wbs, resources, skills)
        for edge in self.dependencies:
            for end in (edge.src_task_id, edge.dst_task_id):
                if end not in tasks:
                    issues.append(f"dependency references unknown task {end}")
            if edge.src_task_id == edge.dst_task_id:
                issues.append(f"dependency on task {edge.src_task_id} points to itself")
        for row in self.capacity_exceptions:
            owner = resources.get(row.resource_id)
            if owner is None:
                issues.append(f"capacity exception references unknown resource {row.resource_id}")
            elif row.units_available > owner.capacity_units:
                issues.append(f"capacity exception on {row.resource_id} exceeds capacity")
        if self.baseline is not None:
            for task_id in self.baseline.task_dates:
                if task_id not in tasks:
                    issues.append(f"baseline references unknown task {task_id}")
        _unique(issues, "risk driver", [row.id for row in self.risk_drivers])
        for driver in self.risk_drivers:
            for task_id in driver.task_ids:
                if task_id not in tasks:
                    issues.append(f"risk driver {driver.id} references unknown task {task_id}")
        if issues:
            raise ValueError("; ".join(issues))
        issues.extend(_graph_issues(self))
        if issues:
            raise ValueError("; ".join(issues))
        return self

    def task(self, task_id: str) -> Task:
        for row in self.tasks:
            if row.id == task_id:
                return row
        raise KeyError(task_id)

    def reference_dates(self) -> dict[str, tuple[date, date]]:
        """Dates a new plan is compared against: baseline first, else source plan."""
        out: dict[str, tuple[date, date]] = {}
        for task in self.tasks:
            if self.baseline is not None and task.id in self.baseline.task_dates:
                row = self.baseline.task_dates[task.id]
                out[task.id] = (row.start, row.finish)
            elif task.baseline_start is not None and task.baseline_finish is not None:
                out[task.id] = (task.baseline_start, task.baseline_finish)
            elif task.planned_start is not None and task.planned_finish is not None:
                out[task.id] = (task.planned_start, task.planned_finish)
        return out


def _unique(issues: list[str], label: str, ids: list[str]) -> None:
    seen: set[str] = set()
    for ident in ids:
        if ident in seen:
            issues.append(f"duplicate {label} id {ident}")
        seen.add(ident)


def _task_refs(
    issues: list[str],
    task: Task,
    projects: set[str],
    wbs: dict[str, WBSNode],
    resources: dict[str, Resource],
    skills: set[str],
) -> None:
    if task.project_id not in projects:
        issues.append(f"task {task.id} references unknown project {task.project_id}")
    if task.wbs_id is not None:
        node = wbs.get(task.wbs_id)
        if node is None:
            issues.append(f"task {task.id} references unknown wbs {task.wbs_id}")
        elif node.project_id != task.project_id:
            issues.append(f"task {task.id} wbs {task.wbs_id} belongs to another project")
    for demand in task.demands:
        if demand.resource_id is not None:
            resource = resources.get(demand.resource_id)
            if resource is None:
                issues.append(f"task {task.id} demands unknown resource {demand.resource_id}")
            elif demand.units > resource.capacity_units:
                issues.append(
                    f"task {task.id} demands {demand.units} units of {resource.id} "
                    f"(capacity {resource.capacity_units})"
                )
        if demand.skill_id is not None and demand.skill_id not in skills:
            issues.append(f"task {task.id} demands unknown skill {demand.skill_id}")


def skill_pool_conflicts(program: OKRProgram) -> list[str]:
    """People that make pooled skill planning inexact.

    Pools are exact (a skill demand is a claim on the department's capacity)
    when every member of a demanded skill belongs to exactly one demanded skill
    and is never demanded by name. Otherwise the pooled model is only a
    relaxation and every skill demand must be bound to a named person.
    """
    demanded = {d.skill_id for t in program.tasks for d in t.demands if d.skill_id is not None}
    named = {d.resource_id for t in program.tasks for d in t.demands if d.resource_id is not None}
    out: list[str] = []
    for resource in program.resources:
        pools = [skill for skill in resource.skills if skill in demanded]
        if len(pools) > 1 or (pools and resource.id in named):
            out.append(resource.id)
    return out


def difference_constraints(program: OKRProgram) -> list[tuple[str, str, int]]:
    """Dependencies as ``start(dst) - start(src) >= w`` triples (working days)."""
    durations = {task.id: task.duration_wd for task in program.tasks}
    out: list[tuple[str, str, int]] = []
    for edge in program.dependencies:
        if not edge.hard:
            continue
        d_src = durations[edge.src_task_id]
        d_dst = durations[edge.dst_task_id]
        base = _anchor_offset(edge.type, d_src, d_dst)
        out.append((edge.src_task_id, edge.dst_task_id, base + edge.lag_wd))
        if edge.max_lag_wd is not None:
            out.append((edge.dst_task_id, edge.src_task_id, -(base + edge.max_lag_wd)))
    return out


def _anchor_offset(kind: DependencyType, d_src: int, d_dst: int) -> int:
    """``start(dst) - start(src)`` offset implied by a zero-lag relation."""
    if kind is DependencyType.FS:
        return d_src
    if kind is DependencyType.SS:
        return 0
    if kind is DependencyType.FF:
        return d_src - d_dst
    return -d_dst  # SF


def _graph_issues(program: OKRProgram) -> list[str]:
    issues: list[str] = []
    succ: dict[str, list[str]] = defaultdict(list)
    indeg = {task.id: 0 for task in program.tasks}
    for edge in program.dependencies:
        if not edge.hard:
            continue
        succ[edge.src_task_id].append(edge.dst_task_id)
        indeg[edge.dst_task_id] += 1
    frontier = [node for node, degree in indeg.items() if degree == 0]
    while frontier:
        node = frontier.pop()
        for nxt in succ.get(node, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                frontier.append(nxt)
    cyclic = sorted(node for node, degree in indeg.items() if degree > 0)
    if cyclic:
        issues.append(f"dependency graph has a cycle through tasks {cyclic[:6]}")
    # Bellman-Ford is exact: a listed cycle is a proven contradiction, an empty
    # result means the max lags are feasible. A structural cycle is refused even
    # when the weights compensate, because scheduling walks an acyclic link graph.
    if any(edge.max_lag_wd is not None for edge in program.dependencies):
        cycle = positive_cycle(program)
        if cycle:
            issues.append(f"max lags create a positive cycle through tasks {cycle[:6]}")
    return issues


def positive_cycle(program: OKRProgram) -> list[str]:
    """Bellman-Ford on the difference-constraint graph; non-empty when infeasible."""
    nodes = [task.id for task in program.tasks]
    constraints = difference_constraints(program)
    dist = dict.fromkeys(nodes, 0)
    for _ in range(len(nodes)):
        changed = False
        for src, dst, weight in constraints:
            if dist[src] + weight > dist[dst]:
                dist[dst] = dist[src] + weight
                changed = True
        if not changed:
            return []
    return sorted({dst for src, dst, weight in constraints if dist[src] + weight > dist[dst]})
