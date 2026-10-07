"""Compile an ``OKRProgram`` into a SynAPS ``ScheduleProblem``.

Mapping (one kernel minute = one working day of the program calendar):

* active task                      -> Operation, duration = working days
* maximal FS/0 same-project chain  -> Order + one unary WorkCenter
* any other dependency             -> native ``PrecedenceEdge`` (FS/SS/FF/SF, lags)
* person / stand / lab / group     -> AuxiliaryResource, pool = capacity units
* skill demand                     -> skill-pool AuxiliaryResource (bound to a
                                      concrete person after the solve)
* vacation / maintenance / closed  -> fixed "block" operations holding the
  resource calendar day                lost units of the resource and its pools
* DONE task                        -> not scheduled; its dates become bounds
* IN_PROGRESS task                 -> remaining work pinned at the status date
* pinned / frozen / shift limit    -> earliest_start / latest_finish window
* deadline / latest_finish         -> hard latest_finish
* due date                         -> soft: own Order with that due date
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from synaps.model import (
    AuxiliaryResource,
    Operation,
    OperationAuxRequirement,
    Order,
    ScheduleProblem,
    State,
    WorkCenter,
)
from synaps.model import (
    ModeRequirement as KernelDemand,
)
from synaps.model import (
    OperationMode as KernelMode,
)
from synaps.precedence import PrecedenceEdge, PrecedenceType

from synaps_programplan.calendar import DayCounter, WorkCalendar, WorkdayAxis
from synaps_programplan.model import (
    Dependency,
    DependencyType,
    OKRProgram,
    Project,
    Task,
    TaskStatus,
)

EdgeMode = Literal["native", "windows"]

_NS = uuid5(NAMESPACE_URL, "https://github.com/KonkovDV/SynAPS-ProgramPlan")


def sid(*parts: str) -> UUID:
    return uuid5(_NS, "\x1f".join(parts))


def program_calendar(program: OKRProgram) -> WorkCalendar:
    row = next(c for c in program.calendars if c.id == program.program.calendar_id)
    return WorkCalendar.from_model(row)


def resource_calendar(program: OKRProgram, resource_id: str) -> WorkCalendar | None:
    resource = next(r for r in program.resources if r.id == resource_id)
    if resource.calendar_id is None or resource.calendar_id == program.program.calendar_id:
        return None
    row = next(c for c in program.calendars if c.id == resource.calendar_id)
    return WorkCalendar.from_model(row)


def anchor_is_end_src(kind: DependencyType) -> bool:
    return kind in (DependencyType.FS, DependencyType.FF)


def anchor_is_end_dst(kind: DependencyType) -> bool:
    return kind in (DependencyType.FF, DependencyType.SF)


@dataclass
class TaskWindow:
    """Bounds on the axis: start >= ``lo``; end boundary <= ``hi``."""

    lo: int
    hi: int
    duration: int
    reasons_lo: list[str] = field(default_factory=list)
    reasons_hi: list[str] = field(default_factory=list)
    fixed: bool = False


@dataclass
class Compiled:
    """Kernel problem plus everything needed to decode and explain it."""

    problem: ScheduleProblem
    axis: WorkdayAxis
    origin: datetime
    base_ordinal: int
    task_op: dict[str, UUID]
    op_task: dict[UUID, str]
    windows: dict[str, TaskWindow]
    active: list[str]
    fixed_anchor: dict[str, tuple[int | None, int | None]]
    resource_aux: dict[str, UUID]
    skill_aux: dict[str, UUID]
    skill_members: dict[str, list[str]]
    availability: dict[str, list[int]]
    edge_mode: EdgeMode
    cross_edges: list[Dependency]
    approximated: list[Dependency]
    infeasible_windows: list[str]
    block_ops: set[UUID]
    # task -> mode code -> aux uuid -> units. Empty when the program has one way per task.
    mode_demand: dict[str, dict[str, dict[UUID, int]]] = field(default_factory=dict)
    selected_mode: dict[str, str] = field(default_factory=dict)

    def offset(self, index: int) -> datetime:
        return self.origin + timedelta(minutes=index)

    def index_of(self, moment: datetime) -> int:
        return round((moment - self.origin).total_seconds() / 60.0)


def _origin(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=UTC)


def compile_program(
    program: OKRProgram,
    *,
    edge_mode: EdgeMode = "native",
    extra_lo: dict[str, int] | None = None,
    extra_hi: dict[str, int] | None = None,
    capacity_scale: dict[str, float] | None = None,
    zero_due_sinks: bool = False,
    due_override: dict[str, int] | None = None,
    ignore_due_projects: frozenset[str] = frozenset(),
) -> Compiled:
    """Build the kernel problem. ``extra_*`` tighten windows (scenarios, repair).

    ``zero_due_sinks`` gives every terminal task without its own due date a due
    date at the status date, so kernel tardiness becomes the sum of project
    completion times (the kernel makespan also counts fixed calendar blocks).
    ``due_override`` (task -> end boundary) gives tasks their own soft due date;
    such tasks are never merged into chains, so the kernel sees each one.
    ``ignore_due_projects``: soft due dates of these projects are dropped, so
    the objective only weighs the other projects (hard deadlines stay).
    """
    calendar = program_calendar(program)
    planning_start = program.program.planning_start
    axis = WorkdayAxis.build(calendar, planning_start, program.program.horizon_end)
    counter = DayCounter(calendar)
    base = counter.ordinal_on_or_after(planning_start)
    tasks = {task.id: task for task in program.tasks}

    fixed_anchor: dict[str, tuple[int | None, int | None]] = {}
    active: list[str] = []
    for task in program.tasks:
        if task.status is TaskStatus.DONE:
            assert task.actual_start is not None and task.actual_finish is not None
            end = counter.boundary_after(task.actual_finish) - base
            start = end if task.duration_wd == 0 else (counter.ordinal_on_or_after(task.actual_start) - base)
            fixed_anchor[task.id] = (start, end)
        elif task.status is TaskStatus.IN_PROGRESS:
            assert task.actual_start is not None
            fixed_anchor[task.id] = (counter.ordinal_on_or_after(task.actual_start) - base, None)
            active.append(task.id)
        else:
            active.append(task.id)

    refs = program.reference_dates()
    projects = {project.id: project for project in program.projects}
    windows = {
        task_id: _base_window(
            program,
            tasks[task_id],
            axis,
            base,
            counter,
            refs.get(task_id),
            projects[tasks[task_id].project_id].deadline,
        )
        for task_id in active
    }
    for task_id, lo in (extra_lo or {}).items():
        if task_id in windows and lo > windows[task_id].lo:
            windows[task_id].lo = lo
            windows[task_id].reasons_lo.append("scenario")
    for task_id, hi in (extra_hi or {}).items():
        if task_id in windows and hi < windows[task_id].hi:
            windows[task_id].hi = hi
            windows[task_id].reasons_hi.append("scenario")

    native: list[Dependency] = []
    cross: list[Dependency] = []
    approximated: list[Dependency] = []
    for edge in program.dependencies:
        if not edge.hard:
            continue
        src_fixed = fixed_anchor.get(edge.src_task_id)
        dst_fixed = fixed_anchor.get(edge.dst_task_id)
        if edge.dst_task_id not in windows:
            continue  # destination already DONE: history
        dst_start_fixed = dst_fixed is not None and not anchor_is_end_dst(edge.type)
        if dst_start_fixed:
            continue  # destination already started: the start anchor is history
        src_value = _fixed_src_value(edge, src_fixed)
        if src_value is not None:
            _apply_fixed_bound(windows[edge.dst_task_id], edge, src_value)
            continue
        native.append(edge)
    segments, rest = _segments(program, active, native, set(due_override or {}))
    if edge_mode == "windows":
        cross, rest = rest, []
        approximated = [e for e in cross if e.max_lag_wd is not None]

    infeasible = [
        f"{task_id}: window [{w.lo}, {w.hi}) cannot hold {w.duration} working days"
        for task_id, w in windows.items()
        if w.lo + w.duration > w.hi
    ]
    problem_parts = _build_kernel(
        program,
        axis=axis,
        origin=_origin(planning_start),
        base=base,
        counter=counter,
        windows=windows,
        active=active,
        segments=segments,
        kernel_edges=rest,
        all_edges=native,
        capacity_scale=capacity_scale or {},
        zero_due_sinks=zero_due_sinks,
        due_override=due_override or {},
        ignore_due_projects=ignore_due_projects,
    )
    return Compiled(
        axis=axis,
        origin=_origin(planning_start),
        base_ordinal=base,
        windows=windows,
        active=active,
        fixed_anchor=fixed_anchor,
        edge_mode=edge_mode,
        cross_edges=cross,
        approximated=approximated,
        infeasible_windows=infeasible,
        **problem_parts,
    )


def _chainable(edge: Dependency) -> bool:
    return edge.type is DependencyType.FS and edge.lag_wd == 0 and edge.max_lag_wd is None


def _kernel_relation(edge: Dependency, durations: dict[str, int]) -> DependencyType:
    """Map a domain link onto kernel anchors.

    The kernel reserves at least one minute for every operation, so a milestone
    (domain duration 0) is stored as an instant at the kernel START; its kernel
    end is one minute later and must not be used as an anchor. End-anchored
    sides of a milestone are therefore rewritten onto that start.
    """
    src_end = anchor_is_end_src(edge.type) and durations[edge.src_task_id] > 0
    dst_end = anchor_is_end_dst(edge.type) and durations[edge.dst_task_id] > 0
    return {
        (True, False): DependencyType.FS,
        (False, False): DependencyType.SS,
        (True, True): DependencyType.FF,
        (False, True): DependencyType.SF,
    }[(src_end, dst_end)]


def _fixed_src_value(edge: Dependency, src_fixed: tuple[int | None, int | None] | None) -> int | None:
    if src_fixed is None:
        return None
    start, end = src_fixed
    return end if anchor_is_end_src(edge.type) else start


def _apply_fixed_bound(window: TaskWindow, edge: Dependency, src_value: int) -> None:
    lower = src_value + edge.lag_wd
    if anchor_is_end_dst(edge.type):
        lower -= window.duration
    if lower > window.lo:
        window.lo = lower
        window.reasons_lo.append(f"dep:{edge.src_task_id}")
    if edge.max_lag_wd is not None:
        upper = src_value + edge.max_lag_wd
        if not anchor_is_end_dst(edge.type):
            upper += window.duration
        if upper < window.hi:
            window.hi = upper
            window.reasons_hi.append(f"maxlag:{edge.src_task_id}")


def _base_window(
    program: OKRProgram,
    task: Task,
    axis: WorkdayAxis,
    base: int,
    counter: DayCounter,
    reference: tuple[date, date] | None,
    project_deadline: date | None,
) -> TaskWindow:
    duration = min(mode.duration_wd for mode in task.modes) if task.modes else task.duration_wd
    if task.status is TaskStatus.IN_PROGRESS:
        elapsed = max(0, -(counter.ordinal_on_or_after(task.actual_start or axis.days[0]) - base))
        remaining = task.remaining_wd if task.remaining_wd is not None else duration - elapsed
        duration = max(0, remaining)
        return TaskWindow(
            lo=0,
            hi=duration,
            duration=duration,
            reasons_lo=["in_progress"],
            reasons_hi=["in_progress"],
            fixed=True,
        )
    milestone = duration == 0
    lo = 1 if milestone else 0
    window = TaskWindow(
        lo=lo, hi=len(axis), duration=duration, reasons_lo=["status_date"], reasons_hi=["horizon"]
    )
    if task.earliest_start is not None:
        bound = axis.index_on_or_after(task.earliest_start) + (1 if milestone else 0)
        if bound > window.lo:
            window.lo = bound
            window.reasons_lo.append("earliest_start")
    for label, bound_date in (("deadline", task.hard_finish), ("project_deadline", project_deadline)):
        if bound_date is not None:
            bound = axis.boundary_after(bound_date)
            if bound < window.hi:
                window.hi = bound
                window.reasons_hi.append(label)
    if reference is not None:
        ref_start = _ref_start_index(axis, reference[0], reference[1], milestone)
        frozen = program.freeze.freeze_until is not None and reference[0] < program.freeze.freeze_until
        if (task.pinned and program.freeze.freeze_pinned) or frozen:
            window.lo = ref_start
            window.hi = ref_start + duration
            window.fixed = True
            window.reasons_lo.append("pinned" if task.pinned else "frozen")
            window.reasons_hi.append("pinned" if task.pinned else "frozen")
        elif task.shift_limit_wd is not None:
            delta = task.shift_limit_wd
            if ref_start - delta > window.lo:
                window.lo = ref_start - delta
                window.reasons_lo.append("shift_limit")
            if ref_start + delta + duration < window.hi:
                window.hi = ref_start + delta + duration
                window.reasons_hi.append("shift_limit")
    return window


def _ref_start_index(axis: WorkdayAxis, start: date, finish: date, milestone: bool) -> int:
    if milestone:
        return max(1, axis.boundary_after(finish))
    return axis.index_on_or_after(start)


def reference_index(compiled: Compiled, program: OKRProgram) -> dict[str, int]:
    """Reference start index per active task (baseline / source plan)."""
    out: dict[str, int] = {}
    for task_id, (start, finish) in program.reference_dates().items():
        window = compiled.windows.get(task_id)
        if window is None:
            continue
        out[task_id] = _ref_start_index(compiled.axis, start, finish, window.duration == 0)
    return out


def _segments(
    program: OKRProgram, active: list[str], native: list[Dependency], forced_due: set[str]
) -> tuple[list[list[str]], list[Dependency]]:
    """Maximal FS/0 same-project chains; every other active edge stays native."""
    tasks = {task.id: task for task in program.tasks}
    projects = {project.id: project for project in program.projects}
    succ: dict[str, list[Dependency]] = defaultdict(list)
    pred: dict[str, list[Dependency]] = defaultdict(list)
    for edge in native:
        succ[edge.src_task_id].append(edge)
        pred[edge.dst_task_id].append(edge)
    has_due = forced_due | {
        task_id for task_id in active if _soft_due(tasks[task_id], projects, succ) is not None
    }
    chain_next: dict[str, str] = {}
    chain_edges: set[int] = set()
    for edge in native:
        src, dst = edge.src_task_id, edge.dst_task_id
        if (
            _chainable(edge)
            and tasks[src].duration_wd > 0
            and tasks[dst].duration_wd > 0
            and tasks[src].project_id == tasks[dst].project_id
            and len(succ[src]) == 1
            and len(pred[dst]) == 1
            and src not in has_due
        ):
            chain_next[src] = dst
            chain_edges.add(id(edge))
    chained = set(chain_next.values())
    heads = [task_id for task_id in active if task_id not in chained]
    segments: list[list[str]] = []
    for head in heads:
        chain = [head]
        while chain[-1] in chain_next:
            chain.append(chain_next[chain[-1]])
        segments.append(chain)
    covered = sum(len(chain) for chain in segments)
    if covered != len(active):
        raise ValueError("segmentation lost tasks (cyclic chain?)")
    rest = [edge for edge in native if id(edge) not in chain_edges]
    return segments, rest


def _soft_due(task: Task, projects: dict[str, Project], succ: dict[str, list[Dependency]]) -> date | None:
    if task.due_date is not None:
        return task.due_date
    project_due = projects[task.project_id].due_date
    if project_due is not None and not succ.get(task.id):
        return project_due  # project sinks carry the project due date
    return None


def _build_kernel(
    program: OKRProgram,
    *,
    axis: WorkdayAxis,
    origin: datetime,
    base: int,
    counter: DayCounter,
    windows: dict[str, TaskWindow],
    active: list[str],
    segments: list[list[str]],
    kernel_edges: list[Dependency],
    all_edges: list[Dependency],
    capacity_scale: dict[str, float],
    zero_due_sinks: bool,
    due_override: dict[str, int],
    ignore_due_projects: frozenset[str],
) -> dict[str, Any]:
    tasks = {task.id: task for task in program.tasks}
    projects = {project.id: project for project in program.projects}
    horizon = len(axis)
    # Milestones need one extra minute past the last boundary (kernel grain is >= 1).
    has_milestone = any(windows[task_id].duration == 0 for task_id in active)
    horizon_end = origin + timedelta(minutes=horizon + (1 if has_milestone else 0))
    state = State(id=sid("state", "work"), code="work")
    succ_all: dict[str, list[Dependency]] = defaultdict(list)
    for edge in all_edges:
        succ_all[edge.src_task_id].append(edge)

    task_op = {task_id: sid("op", task_id) for task_id in active}
    orders: list[Order] = []
    operations: list[Operation] = []
    centers: list[WorkCenter] = []
    for chain in segments:
        head = chain[0]
        order_id = sid("order", head)
        wc_id = sid("wc", head)
        tail = 1 if windows[chain[-1]].duration == 0 else 0
        due = _soft_due(tasks[chain[-1]], projects, succ_all)
        if due is not None:
            due_dt = origin + timedelta(minutes=axis.boundary_after(due) + tail)
        elif zero_due_sinks and not succ_all.get(chain[-1]):
            due_dt = origin + timedelta(minutes=tail)
        else:
            due_dt = horizon_end
        if chain[-1] in due_override:
            due_dt = min(due_dt, origin + timedelta(minutes=max(0, due_override[chain[-1]]) + tail))
        if tasks[chain[-1]].project_id in ignore_due_projects:
            due_dt = horizon_end
        orders.append(
            Order(
                id=order_id,
                external_ref=f"seg:{head}",
                due_date=due_dt,
                priority=projects[tasks[head].project_id].priority,
                domain_attributes={"tasks": list(chain)},
            )
        )
        centers.append(WorkCenter(id=wc_id, code=f"lane:{head}", capability_group="okr"))
        previous: UUID | None = None
        for seq, task_id in enumerate(chain):
            window = windows[task_id]
            tail = 1 if window.duration == 0 else 0
            operations.append(
                Operation(
                    id=task_op[task_id],
                    order_id=order_id,
                    seq_in_order=seq,
                    state_id=state.id,
                    base_duration_min=window.duration,
                    eligible_wc_ids=[wc_id],
                    predecessor_op_id=previous,
                    earliest_start=origin + timedelta(minutes=max(0, window.lo)),
                    latest_finish=origin + timedelta(minutes=min(window.hi, horizon) + tail),
                    modes=_kernel_modes(tasks[task_id]),
                    domain_attributes={"task_id": task_id},
                )
            )
            previous = task_op[task_id]

    resource_aux, skill_aux, skill_members, availability, aux, reqs, blocks = _resources(
        program, axis, counter, base, active, windows, task_op, capacity_scale
    )
    block_ops: set[UUID] = set()
    for resource_key, runs in blocks.items():
        if not runs:
            continue
        order_id = sid("order", "block", resource_key)
        wc_id = sid("wc", "block", resource_key)
        orders.append(Order(id=order_id, external_ref=f"block:{resource_key}", due_date=horizon_end))
        centers.append(WorkCenter(id=wc_id, code=f"block:{resource_key}", capability_group="block"))
        previous = None
        for seq, (start, end, units, aux_id) in enumerate(runs):
            op_id = sid("op", "block", resource_key, str(seq))
            block_ops.add(op_id)
            operations.append(
                Operation(
                    id=op_id,
                    order_id=order_id,
                    seq_in_order=seq,
                    state_id=state.id,
                    base_duration_min=end - start,
                    eligible_wc_ids=[wc_id],
                    predecessor_op_id=previous,
                    earliest_start=origin + timedelta(minutes=start),
                    latest_finish=origin + timedelta(minutes=end),
                    domain_attributes={"block": resource_key},
                )
            )
            reqs.append(
                OperationAuxRequirement(operation_id=op_id, aux_resource_id=aux_id, quantity_needed=units)
            )
            previous = op_id

    durations = {task_id: windows[task_id].duration for task_id in active}
    kernel_view = [
        edge.model_copy(update={"type": _kernel_relation(edge, durations)}) for edge in kernel_edges
    ]
    edges = [
        PrecedenceEdge(
            src_op_id=task_op[edge.src_task_id],
            dst_op_id=task_op[edge.dst_task_id],
            type=PrecedenceType(edge.type.value),
            min_lag=edge.lag_wd,
            max_lag=edge.max_lag_wd,
        )
        for edge in _dedupe_edges(kernel_view)
    ]
    problem = ScheduleProblem(
        states=[state],
        orders=orders,
        operations=operations,
        work_centers=centers,
        setup_matrix=[],
        auxiliary_resources=aux,
        aux_requirements=reqs,
        precedence_edges=edges,
        planning_horizon_start=origin,
        planning_horizon_end=horizon_end,
    )
    return {
        "problem": problem,
        "task_op": task_op,
        "op_task": {op: task for task, op in task_op.items()},
        "resource_aux": resource_aux,
        "skill_aux": skill_aux,
        "skill_members": skill_members,
        "availability": availability,
        "block_ops": block_ops,
        "mode_demand": _mode_demand(program, active),
    }


def _dedupe_edges(edges: list[Dependency]) -> list[Dependency]:
    """The kernel forbids duplicate (src, dst, type); keep the tightest bounds."""
    merged: dict[tuple[str, str, DependencyType], Dependency] = {}
    for edge in edges:
        key = (edge.src_task_id, edge.dst_task_id, edge.type)
        current = merged.get(key)
        if current is None:
            merged[key] = edge
            continue
        max_lags = [lag for lag in (current.max_lag_wd, edge.max_lag_wd) if lag is not None]
        merged[key] = current.model_copy(
            update={
                "lag_wd": max(current.lag_wd, edge.lag_wd),
                "max_lag_wd": min(max_lags) if max_lags else None,
            }
        )
    return list(merged.values())


def daily_availability(
    program: OKRProgram, axis: WorkdayAxis, counter: DayCounter, base: int
) -> dict[str, list[int]]:
    """Available units per resource and axis day (exceptions + own calendar)."""
    out: dict[str, list[int]] = {}
    for resource in program.resources:
        days = [resource.capacity_units] * len(axis)
        own = resource_calendar(program, resource.id)
        if own is not None:
            for index, day in enumerate(axis.days):
                if not own.is_workday(day):
                    days[index] = 0
        out[resource.id] = days
    for row in program.capacity_exceptions:
        days = out[row.resource_id]
        first = axis.index_on_or_after(row.start)
        last = axis.boundary_after(row.end)
        for index in range(first, last):
            days[index] = min(days[index], row.units_available)
    return out


def _resources(
    program: OKRProgram,
    axis: WorkdayAxis,
    counter: DayCounter,
    base: int,
    active: list[str],
    windows: dict[str, TaskWindow],
    task_op: dict[str, UUID],
    capacity_scale: dict[str, float],
) -> tuple[
    dict[str, UUID],
    dict[str, UUID],
    dict[str, list[str]],
    dict[str, list[int]],
    list[AuxiliaryResource],
    list[OperationAuxRequirement],
    dict[str, list[tuple[int, int, int, UUID]]],
]:
    tasks = {task.id: task for task in program.tasks}
    availability = daily_availability(program, axis, counter, base)
    resource_aux = {r.id: sid("res", r.id) for r in program.resources}
    aux: list[AuxiliaryResource] = []
    blocks: dict[str, list[tuple[int, int, int, UUID]]] = {}
    capacity: dict[str, int] = {}
    for resource in program.resources:
        scale = capacity_scale.get(resource.id, 1.0)
        cap = max(1, int(resource.capacity_units * scale))
        capacity[resource.id] = cap
        aux.append(
            AuxiliaryResource(
                id=resource_aux[resource.id],
                code=resource.code,
                resource_type=resource.kind.value.lower(),
                pool_size=resource.capacity_units,
                domain_attributes={"resource_id": resource.id},
            )
        )
        lost = [resource.capacity_units - min(cap, units) for units in availability[resource.id]]
        blocks[f"res:{resource.id}"] = [
            (start, end, units, resource_aux[resource.id]) for start, end, units in _runs(lost)
        ]

    skill_members: dict[str, list[str]] = {}
    skill_aux: dict[str, UUID] = {}
    demanded_skills = sorted(
        {d.skill_id for t in program.tasks for d in t.all_demands() if d.skill_id is not None}
    )
    for skill_id in demanded_skills:
        members = sorted(r.id for r in program.resources if skill_id in r.skills)
        skill_members[skill_id] = members
        if not members:
            continue
        skill_aux[skill_id] = sid("skill", skill_id)
        pool = sum(r.capacity_units for r in program.resources if r.id in members)
        aux.append(
            AuxiliaryResource(
                id=skill_aux[skill_id],
                code=f"skill:{skill_id}",
                resource_type="skill_pool",
                pool_size=pool,
                domain_attributes={"skill_id": skill_id, "members": members},
            )
        )
        lost = [
            pool - sum(min(capacity[m], availability[m][day]) for m in members) for day in range(len(axis))
        ]
        blocks[f"skill:{skill_id}"] = [
            (start, end, units, skill_aux[skill_id]) for start, end, units in _runs(lost)
        ]

    reqs: list[OperationAuxRequirement] = []
    for task_id in active:
        if windows[task_id].duration == 0 or tasks[task_id].modes:
            continue
        units_by_aux: dict[UUID, int] = defaultdict(int)
        for demand in tasks[task_id].demands:
            if demand.resource_id is not None:
                units_by_aux[resource_aux[demand.resource_id]] += demand.units
            elif demand.skill_id in skill_aux:
                units_by_aux[skill_aux[demand.skill_id]] += demand.units
        for aux_id, units in units_by_aux.items():
            reqs.append(
                OperationAuxRequirement(
                    operation_id=task_op[task_id], aux_resource_id=aux_id, quantity_needed=units
                )
            )
    return resource_aux, skill_aux, skill_members, availability, aux, reqs, blocks


def _kernel_modes(task: Task) -> list[KernelMode]:
    if not task.modes:
        return []
    out: list[KernelMode] = []
    for mode in task.modes:
        requirements: list[KernelDemand] = []
        for demand in mode.demands:
            aux_id = (
                sid("res", demand.resource_id) if demand.resource_id else sid("skill", demand.skill_id or "")
            )
            requirements.append(KernelDemand(aux_resource_id=aux_id, quantity_needed=demand.units))
        out.append(KernelMode(code=mode.code, duration_min=mode.duration_wd, requirements=requirements))
    return out


def _mode_demand(program: OKRProgram, active: list[str]) -> dict[str, dict[str, dict[UUID, int]]]:
    tasks = {task.id: task for task in program.tasks}
    out: dict[str, dict[str, dict[UUID, int]]] = {}
    for task_id in active:
        task = tasks[task_id]
        if not task.modes:
            continue
        out[task_id] = {}
        for mode in task.modes:
            bucket: dict[UUID, int] = {}
            for demand in mode.demands:
                aux_id = (
                    sid("res", demand.resource_id)
                    if demand.resource_id
                    else sid("skill", demand.skill_id or "")
                )
                bucket[aux_id] = demand.units
            out[task_id][mode.code] = bucket
    return out


def _runs(lost: list[int]) -> list[tuple[int, int, int]]:
    """Maximal runs of equal positive lost units: ``(start, end, units)``."""
    out: list[tuple[int, int, int]] = []
    index = 0
    while index < len(lost):
        units = lost[index]
        if units <= 0:
            index += 1
            continue
        end = index
        while end < len(lost) and lost[end] == units:
            end += 1
        out.append((index, end, units))
        index = end
    return out
