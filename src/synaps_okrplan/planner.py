"""Facade: compile -> SynAPS solve -> decode -> left-shift -> double check -> evidence.

A plan is accepted (``outcome.ok``) only when the solver status is feasible,
the SynAPS FeasibilityChecker passes on the FINAL assignments, and the
independent domain checker reports no hard violation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import UUID

from synaps.model import Assignment, ScheduleResult, SolverStatus
from synaps.portfolio import PortfolioValidationError, solve_schedule
from synaps.validation import verify_schedule_result

from synaps_okrplan.binding import Binding, bind_exact
from synaps_okrplan.checker import check_plan
from synaps_okrplan.compiler import (
    Compiled,
    EdgeMode,
    anchor_is_end_dst,
    anchor_is_end_src,
    compile_program,
    reference_index,
)
from synaps_okrplan.cpm import cpm
from synaps_okrplan.evidence import evidence_stamp, fingerprint
from synaps_okrplan.model import Dependency, OKRProgram, TaskStatus, skill_pool_conflicts
from synaps_okrplan.result import (
    KPI,
    Claim,
    MilestoneKPI,
    Outcome,
    PlanResult,
    ResourceKPI,
    Severity,
    TaskPlan,
    Violation,
)

Objective = Literal["due", "finish", "stability"]


@dataclass(frozen=True)
class SolveConfig:
    solver: Literal["cpsat", "greedy"] = "cpsat"
    time_limit_s: int = 20
    seed: int = 42
    objective: Objective = "finish"
    edge_mode: EdgeMode = "native"
    max_fixpoint_iter: int = 30
    compact: bool = True

    def solver_config(self) -> str:
        return "GREED" if self.solver == "greedy" else "CPSAT-30"

    def solve_kwargs(self) -> dict[str, Any]:
        if self.solver == "greedy":
            return {}
        return {
            "time_limit_s": self.time_limit_s,
            "random_seed": self.seed,
            "objective_mode": "epsilon_primary",
            "primary_objective": "tardiness",
        }


@dataclass
class Adjustments:
    """Scenario / what-if levers applied at compile time."""

    extra_lo: dict[str, int] = field(default_factory=dict)
    extra_hi: dict[str, int] = field(default_factory=dict)
    capacity_scale: dict[str, float] = field(default_factory=dict)


@dataclass
class SolveRun:
    compiled: Compiled
    result: ScheduleResult
    positions: dict[str, tuple[int, int]]
    iterations: int
    solver_error: str = ""
    restricted: bool = False


def plan(
    program: OKRProgram,
    config: SolveConfig | None = None,
    *,
    scenario_id: str = "base",
    label: str = "Базовый план",
    adjustments: Adjustments | None = None,
    warm_start: PlanResult | None = None,
) -> PlanResult:
    """``warm_start``: an accepted plan whose dates seed CP-SAT (a hint, not a constraint)."""
    config = config or SolveConfig()
    adjustments = adjustments or Adjustments()
    hint = None
    if warm_start is not None and warm_start.outcome.ok:
        hint = {row.task_id: (row.start_index, row.end_index) for row in warm_start.tasks}
    run = solve_positions(program, config, adjustments, hint=hint)
    if not _solved(run):
        return finish_plan(
            program, config, run, scenario_id=scenario_id, label=label, adjustments=adjustments
        )
    positions = left_shift(program, run.compiled, run.positions) if config.compact else run.positions
    binding = bind_exact(program, run.compiled, positions, seed=config.seed)
    if binding.exact or not skill_pool_conflicts(program):
        result = finish_plan(
            program,
            config,
            run,
            scenario_id=scenario_id,
            label=label,
            adjustments=adjustments,
            bound_override=binding.bound,
            positions_override=positions,
        )
        result.metadata["skill_binding"] = "exact" if binding.exact else "pool"
        result.metadata["skill_demands_unnamed"] = len(binding.unbound)
        return result
    named_run = _named_rerun(program, config, adjustments, binding, positions)
    result = finish_plan(
        program,
        config,
        named_run,
        scenario_id=scenario_id,
        label=label,
        adjustments=adjustments,
        bound_override=binding.full,
    )
    result.metadata["skill_binding"] = "two_phase"
    result.metadata["binding_overload_units"] = binding.overload_units
    return result


def _solved(run: SolveRun) -> bool:
    return bool(run.positions) and run.result.status in (SolverStatus.FEASIBLE, SolverStatus.OPTIMAL)


def _named_rerun(
    program: OKRProgram,
    config: SolveConfig,
    adjustments: Adjustments,
    binding: Binding,
    positions: dict[str, tuple[int, int]],
) -> SolveRun:
    """Phase 2: no exact binding for the pooled dates - re-solve with the named people.

    The binding minimises overload, so the kernel only has to move the few
    tasks that collide. The run is marked ``restricted``: the chosen people
    are a heuristic decision, so an INFEASIBLE answer here proves nothing.
    """
    tasks = []
    for task in program.tasks:
        demands = [
            demand.model_copy(
                update={"resource_id": binding.full[task.id][demand.skill_id], "skill_id": None}
            )
            if demand.skill_id is not None and demand.skill_id in binding.full.get(task.id, {})
            else demand
            for demand in task.demands
        ]
        tasks.append(task.model_copy(update={"demands": demands}))
    named_program = OKRProgram.model_validate(program.model_copy(update={"tasks": tasks}).model_dump())
    named_run = solve_positions(named_program, config, adjustments, hint=positions)
    named_run.restricted = True
    return named_run


def solve_positions(
    program: OKRProgram,
    config: SolveConfig,
    adjustments: Adjustments,
    *,
    hint: dict[str, tuple[int, int]] | None = None,
) -> SolveRun:
    extra_lo = dict(adjustments.extra_lo)
    due_override = None
    if config.objective == "stability":
        due_override = _stability_targets(program, extra_lo)
    iterations = 0
    while True:
        iterations += 1
        compiled = compile_program(
            program,
            edge_mode=config.edge_mode,
            extra_lo=extra_lo,
            extra_hi=adjustments.extra_hi,
            capacity_scale=adjustments.capacity_scale,
            zero_due_sinks=config.objective == "finish",
            due_override=due_override,
        )
        if compiled.infeasible_windows:
            empty = ScheduleResult(solver_name="precheck", status=SolverStatus.INFEASIBLE)
            return SolveRun(
                compiled,
                empty,
                {},
                iterations,
                "; ".join(compiled.infeasible_windows[:5]),
                restricted=iterations > 1,
            )
        kwargs = config.solve_kwargs()
        if hint and config.solver == "cpsat":
            kwargs["warm_start_assignments"] = _warm_start(compiled, hint)
        try:
            result = solve_schedule(
                compiled.problem,
                solver_config=config.solver_config(),
                solve_kwargs=kwargs,
                verify_feasibility=True,
            )
        except PortfolioValidationError as exc:
            rejected = ScheduleResult(solver_name=config.solver_config(), status=SolverStatus.ERROR)
            return SolveRun(compiled, rejected, {}, iterations, f"kernel rejected plan: {exc}")
        positions = _positions(compiled, result.assignments)
        if result.status not in (SolverStatus.FEASIBLE, SolverStatus.OPTIMAL) or not compiled.cross_edges:
            return SolveRun(compiled, result, positions, iterations, restricted=iterations > 1)
        lifted = _lift_cross_edges(compiled, positions, extra_lo)
        if not lifted or iterations >= config.max_fixpoint_iter:
            return SolveRun(compiled, result, positions, iterations, restricted=iterations > 1)


def _stability_targets(program: OKRProgram, extra_lo: dict[str, int]) -> dict[str, int]:
    """Stability: no task earlier than approved, soft due = approved end.

    Kernel tardiness then equals the total delay against the approved plan.
    """
    base = compile_program(program)
    targets: dict[str, int] = {}
    for task_id, ref in reference_index(base, program).items():
        window = base.windows.get(task_id)
        if window is None or window.fixed:
            continue
        if ref + window.duration <= window.hi:
            extra_lo[task_id] = max(extra_lo.get(task_id, ref), ref)
        targets[task_id] = ref + window.duration
    return targets


def _warm_start(compiled: Compiled, hint: dict[str, tuple[int, int]]) -> list[Assignment]:
    out: list[Assignment] = []
    for op in compiled.problem.operations:
        task_id = compiled.op_task.get(op.id)
        if task_id is None:
            start = compiled.index_of(op.earliest_start) if op.earliest_start else 0
            end = start + op.base_duration_min
        elif task_id in hint:
            start, end = _kernel_span(compiled, task_id, *hint[task_id])
        else:
            continue
        out.append(
            Assignment(
                operation_id=op.id,
                work_center_id=op.eligible_wc_ids[0],
                start_time=compiled.offset(start),
                end_time=compiled.offset(end),
            )
        )
    return out


def _kernel_span(compiled: Compiled, task_id: str, start: int, end: int) -> tuple[int, int]:
    """Domain instant -> kernel interval. A milestone occupies ``[t, t+1)``."""
    if compiled.windows[task_id].duration == 0:
        return start, start + 1
    return start, end


def _positions(compiled: Compiled, assignments: list[Assignment]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for row in assignments:
        task_id = compiled.op_task.get(row.operation_id)
        if task_id is None:
            continue
        start = compiled.index_of(row.start_time)
        end = compiled.index_of(row.end_time)
        if compiled.windows[task_id].duration == 0:
            end = start
        out[task_id] = (start, end)
    return out


def _lift_cross_edges(
    compiled: Compiled, positions: dict[str, tuple[int, int]], extra_lo: dict[str, int]
) -> bool:
    """RepairFlow-style fixpoint: raise destination windows of violated cross edges."""
    changed = False
    for edge in compiled.cross_edges:
        src = positions.get(edge.src_task_id)
        dst = positions.get(edge.dst_task_id)
        if src is None or dst is None:
            continue
        src_value = src[1] if anchor_is_end_src(edge.type) else src[0]
        duration = compiled.windows[edge.dst_task_id].duration
        need = src_value + edge.lag_wd - (duration if anchor_is_end_dst(edge.type) else 0)
        if dst[0] < need and need > extra_lo.get(edge.dst_task_id, -1):
            extra_lo[edge.dst_task_id] = need
            changed = True
    return changed


# ---------- post-processing ----------


def _active_edges(program: OKRProgram, compiled: Compiled) -> list[Dependency]:
    active = set(compiled.active)
    return [
        edge
        for edge in program.dependencies
        if edge.hard and edge.src_task_id in active and edge.dst_task_id in active
    ]


def _kernel_capacity(compiled: Compiled) -> tuple[dict[UUID, list[int]], dict[str, dict[UUID, int]]]:
    """Free units per aux and day after fixed blocks, and task demand per aux."""
    horizon = len(compiled.axis)
    free = {aux.id: [aux.pool_size] * horizon for aux in compiled.problem.auxiliary_resources}
    ops = {op.id: op for op in compiled.problem.operations}
    demand: dict[str, dict[UUID, int]] = defaultdict(dict)
    for req in compiled.problem.aux_requirements:
        task_id = compiled.op_task.get(req.operation_id)
        if task_id is not None:
            demand[task_id][req.aux_resource_id] = req.quantity_needed
            continue
        op = ops[req.operation_id]
        start = compiled.index_of(op.earliest_start) if op.earliest_start else 0
        for day in range(start, min(horizon, start + op.base_duration_min)):
            free[req.aux_resource_id][day] -= req.quantity_needed
    return free, demand


def left_shift(
    program: OKRProgram, compiled: Compiled, positions: dict[str, tuple[int, int]]
) -> dict[str, tuple[int, int]]:
    """Semi-active compaction: move tasks earlier while every constraint holds.

    Moving a task earlier keeps every outgoing min-lag edge satisfied, so only
    incoming edges, the window and resources are re-checked. Tasks touching a
    max-lag edge, fixed windows and in-progress work are never moved. Objective
    terms (tardiness, finish times) can only improve.
    """
    edges = _active_edges(program, compiled)
    incoming: dict[str, list[Dependency]] = defaultdict(list)
    max_lag_tasks: set[str] = set()
    for edge in edges:
        incoming[edge.dst_task_id].append(edge)
        if edge.max_lag_wd is not None:
            max_lag_tasks.update((edge.src_task_id, edge.dst_task_id))
    free, demand = _kernel_capacity(compiled)
    pos = dict(positions)
    for task_id, (start, end) in pos.items():
        for aux_id, units in demand.get(task_id, {}).items():
            for day in range(start, end):
                free[aux_id][day] -= units
    for _ in range(6):
        moved = False
        for task_id in sorted(pos, key=lambda t: (pos[t][0], t)):
            window = compiled.windows[task_id]
            if window.fixed or task_id in max_lag_tasks:
                continue
            start, end = pos[task_id]
            duration = end - start
            lower = max(window.lo, 0)
            for edge in incoming[task_id]:
                src = pos[edge.src_task_id]
                value = (src[1] if anchor_is_end_src(edge.type) else src[0]) + edge.lag_wd
                lower = max(lower, value - duration if anchor_is_end_dst(edge.type) else value)
            if lower >= start:
                continue
            needs = demand.get(task_id, {})
            for aux_id, units in needs.items():
                for day in range(start, end):
                    free[aux_id][day] += units
            target = _earliest_fit(free, needs, lower, start, duration)
            for aux_id, units in needs.items():
                for day in range(target, target + duration):
                    free[aux_id][day] -= units
            if target < start:
                pos[task_id] = (target, target + duration)
                moved = True
        if not moved:
            break
    return pos


def _earliest_fit(
    free: dict[UUID, list[int]], needs: dict[UUID, int], lower: int, current: int, duration: int
) -> int:
    candidate = lower
    while candidate < current:
        blocked = -1
        for aux_id, units in needs.items():
            row = free[aux_id]
            for day in range(candidate, candidate + duration):
                if row[day] < units:
                    blocked = max(blocked, day)
                    break
        if blocked < 0:
            return candidate
        candidate = blocked + 1
    return current


def _kernel_recheck(
    compiled: Compiled, result: ScheduleResult, positions: dict[str, tuple[int, int]]
) -> tuple[bool, list[str]]:
    rows: list[Assignment] = []
    for row in result.assignments:
        task_id = compiled.op_task.get(row.operation_id)
        if task_id is not None and task_id in positions:
            start, end = _kernel_span(compiled, task_id, *positions[task_id])
            row = row.model_copy(
                update={"start_time": compiled.offset(start), "end_time": compiled.offset(end)}
            )
        rows.append(row)
    final = result.model_copy(update={"assignments": rows})
    verification = verify_schedule_result(compiled.problem, final)
    return verification.feasible, list(verification.violation_kinds)


# ---------- assembly ----------


def finish_plan(
    program: OKRProgram,
    config: SolveConfig,
    run: SolveRun,
    *,
    scenario_id: str,
    label: str,
    adjustments: Adjustments,
    bound_override: dict[str, dict[str, str]] | None = None,
    positions_override: dict[str, tuple[int, int]] | None = None,
) -> PlanResult:
    compiled, result = run.compiled, run.result
    status = result.status
    solved = status in (SolverStatus.FEASIBLE, SolverStatus.OPTIMAL) and bool(run.positions)
    positions = run.positions
    kernel_ok, kernel_kinds = False, list[str]()
    if solved:
        if positions_override is not None:
            positions = positions_override
        elif config.compact:
            positions = left_shift(program, compiled, positions)
        kernel_ok, kernel_kinds = _kernel_recheck(compiled, result, positions)
    if bound_override is not None:
        bound = bound_override if solved else {}
    else:
        bound = bind_exact(program, compiled, positions, seed=config.seed).bound if solved else {}
    rows = build_rows(program, compiled, positions, bound) if solved else []
    violations = check_plan(program, rows) if solved else []
    if compiled.approximated:
        violations.append(
            Violation(
                code="APPROXIMATED_RELATION",
                severity=Severity.WARNING,
                message=f"{len(compiled.approximated)} max-lag relations only checked, not solved",
                task_ids=sorted({e.dst_task_id for e in compiled.approximated})[:20],
            )
        )
    hard = [v for v in violations if v.severity is Severity.HARD]
    ok = solved and kernel_ok and not hard
    claim = _claim(status, ok, config, solved, proof=not run.restricted)
    detail = run.solver_error or ("; ".join(kernel_kinds) if solved and not kernel_ok else "")
    if status is SolverStatus.INFEASIBLE and claim is not Claim.INFEASIBLE:
        detail = detail or (
            "no plan found, but infeasibility is NOT proven: "
            + (
                "the model was restricted heuristically (fixpoint lifts or skill binding)"
                if run.restricted
                else "heuristic solver"
            )
        )
    gap = _gap(result)
    outcome = Outcome(
        ok=ok,
        claim=claim,
        solver_status=status.value,
        solver_config=config.solver_config(),
        kernel_verified=kernel_ok,
        domain_hard_violations=len(hard),
        gap=gap,
        detail=detail,
    )
    if rows:
        _mark_critical(program, compiled, rows)
    kpi = compute_kpi(program, compiled, rows) if rows else None
    config_payload = {"solve": asdict(config), "adjustments": asdict(adjustments)}
    plan_result = PlanResult(
        scenario_id=scenario_id,
        label=label,
        tasks=rows,
        violations=violations,
        outcome=outcome,
        kpi=kpi,
        metadata={
            "iterations": run.iterations,
            "edge_mode": compiled.edge_mode,
            "kernel_operations": len(compiled.problem.operations),
            "kernel_edges": len(compiled.problem.precedence_edges),
            "kernel_orders": len(compiled.problem.orders),
            "axis_days": len(compiled.axis),
            "solve_ms": result.duration_ms,
            "determinism_violated": bool(result.metadata.get("determinism_violated", False)),
            "infeasible_windows": compiled.infeasible_windows[:20],
        },
    )
    plan_result.evidence = evidence_stamp(
        input_hash=fingerprint(program),
        config=config_payload,
        data_provenance=program.provenance.kind.value,
        extra={"plan_hash": plan_hash(plan_result)},
    )
    return plan_result


def plan_hash(result: PlanResult) -> str:
    """Hash of the plan content (dates, binding, violations, verdict); labels excluded."""
    return fingerprint(result.model_dump(mode="json", include={"tasks", "violations", "outcome"}))


def _claim(status: SolverStatus, ok: bool, config: SolveConfig, solved: bool, *, proof: bool) -> Claim:
    if status is SolverStatus.INFEASIBLE:
        # Only exact CP-SAT on the unrestricted model proves infeasibility.
        return Claim.INFEASIBLE if proof and config.solver == "cpsat" else Claim.ERROR
    if not solved:
        return Claim.ERROR
    if not ok:
        return Claim.REJECTED
    if config.solver == "greedy":
        return Claim.HEURISTIC_FEASIBLE
    # OPTIMAL refers to the kernel objective on the compiled problem; the
    # left-shift can only improve it, so the claim survives compaction.
    return Claim.OPTIMAL if status is SolverStatus.OPTIMAL else Claim.FEASIBLE


def _gap(result: ScheduleResult) -> float | None:
    bound = result.metadata.get("best_objective_bound")
    value = result.objective.weighted_sum if result.objective else None
    if not isinstance(bound, int | float) or not value:
        return None
    return max(0.0, (float(value) - float(bound)) / abs(float(value)))


def build_rows(
    program: OKRProgram,
    compiled: Compiled,
    positions: dict[str, tuple[int, int]],
    bound: dict[str, dict[str, str]],
) -> list[TaskPlan]:
    axis = compiled.axis
    refs = program.reference_dates()
    ref_index = reference_index(compiled, program)
    rows: list[TaskPlan] = []
    for task in program.tasks:
        reference = refs.get(task.id)
        if task.status is TaskStatus.DONE:
            assert task.actual_start is not None and task.actual_finish is not None
            start, finish = task.actual_start, task.actual_finish
            s_idx, e_idx = compiled.fixed_anchor[task.id]
            s_idx = s_idx or 0
            e_idx = e_idx or 0
        else:
            s_idx, e_idx = positions[task.id]
            if task.duration_wd == 0:
                start = finish = axis.event_date(e_idx)
            else:
                start = axis.start_date(s_idx)
                finish = axis.finish_date(s_idx, e_idx)
            if task.status is TaskStatus.IN_PROGRESS and task.actual_start is not None:
                start = task.actual_start
        shift = s_idx - ref_index[task.id] if task.id in ref_index and task.id in positions else None
        rows.append(
            TaskPlan(
                task_id=task.id,
                project_id=task.project_id,
                name=task.name,
                start=start,
                finish=finish,
                duration_wd=task.duration_wd,
                status=task.status,
                is_milestone=task.duration_wd == 0,
                bound=bound.get(task.id, {}),
                start_index=s_idx,
                end_index=e_idx,
                reference_start=reference[0] if reference else None,
                reference_finish=reference[1] if reference else None,
                shift_wd=shift,
            )
        )
    return rows


def _mark_critical(program: OKRProgram, compiled: Compiled, rows: list[TaskPlan]) -> None:
    """Precedence + resource links of the final plan; zero float = critical."""
    by_id = {row.task_id: row for row in rows if row.task_id in compiled.windows}
    durations = {task_id: row.end_index - row.start_index for task_id, row in by_id.items()}
    arcs: list[tuple[str, str, int]] = []
    for edge in _active_edges(program, compiled):
        d_src, d_dst = durations[edge.src_task_id], durations[edge.dst_task_id]
        base = {"FS": d_src, "SS": 0, "FF": d_src - d_dst, "SF": -d_dst}[edge.type.value]
        arcs.append((edge.src_task_id, edge.dst_task_id, base + edge.lag_wd))
    arcs.extend(_resource_links(program, by_id))
    lower = {task_id: row.start_index for task_id, row in by_id.items()}
    upper = {task_id: compiled.windows[task_id].hi for task_id in by_id}
    result = cpm(durations, arcs, lower, upper_end=upper)
    for task_id, row in by_id.items():
        slack = result.late_start[task_id] - row.start_index
        row.total_float_wd = slack
        row.critical = slack <= 0


def _resource_links(program: OKRProgram, rows: dict[str, TaskPlan]) -> list[tuple[str, str, int]]:
    """A -> B when B starts exactly when A ends and both use the same resource."""
    users: dict[str, list[TaskPlan]] = defaultdict(list)
    tasks = {task.id: task for task in program.tasks}
    for task_id, row in rows.items():
        for demand in tasks[task_id].demands:
            key = demand.resource_id or row.bound.get(demand.skill_id or "") or ""
            if key:
                users[key].append(row)
    links: list[tuple[str, str, int]] = []
    for group in users.values():
        by_end: dict[int, list[TaskPlan]] = defaultdict(list)
        for row in group:
            by_end[row.end_index].append(row)
        for row in group:
            for previous in by_end.get(row.start_index, []):
                if previous.task_id != row.task_id and previous.end_index > previous.start_index:
                    links.append((previous.task_id, row.task_id, previous.end_index - previous.start_index))
    return links


def compute_kpi(program: OKRProgram, compiled: Compiled, rows: list[TaskPlan]) -> KPI:
    by_id = {row.task_id: row for row in rows}
    project_finish: dict[str, Any] = {}
    for row in rows:
        current = project_finish.get(row.project_id)
        if current is None or row.finish > current:
            project_finish[row.project_id] = row.finish
    milestones: list[MilestoneKPI] = []
    tardiness = 0
    late = 0
    axis = compiled.axis
    for task in program.tasks:
        target = task.hard_finish or task.due_date
        if task.duration_wd != 0 and target is None:
            continue
        row = by_id[task.id]
        lateness = 0
        if target is not None and row.finish > target:
            lateness = max(1, axis.boundary_after(row.finish) - axis.boundary_after(target))
            late += 1
            tardiness += lateness
        milestones.append(
            MilestoneKPI(
                task_id=task.id,
                name=task.name,
                date=row.finish,
                target=target,
                hard=task.hard_finish is not None,
                lateness_wd=lateness,
            )
        )
    resources = _resource_kpis(program, compiled, rows)
    shifts = [abs(row.shift_wd) for row in rows if row.shift_wd]
    return KPI(
        program_finish=max(project_finish.values()) if project_finish else None,
        project_finish=project_finish,
        milestones=milestones,
        late_count=late,
        tardiness_wd=tardiness,
        resources=resources,
        moved_count=len(shifts),
        shift_sum_wd=sum(shifts),
        shift_max_wd=max(shifts, default=0),
    )


def resource_profiles(program: OKRProgram, compiled: Compiled, rows: list[TaskPlan]) -> dict[str, list[int]]:
    horizon = len(compiled.axis)
    tasks = {task.id: task for task in program.tasks}
    load = {resource.id: [0] * horizon for resource in program.resources}
    for row in rows:
        task = tasks[row.task_id]
        if task.status is TaskStatus.DONE:
            continue
        for demand in task.demands:
            rid = demand.resource_id or row.bound.get(demand.skill_id or "")
            if rid is None:
                continue
            for day in range(max(0, row.start_index), min(horizon, row.end_index)):
                load[rid][day] += demand.units
    return load


def _resource_kpis(program: OKRProgram, compiled: Compiled, rows: list[TaskPlan]) -> list[ResourceKPI]:
    load = resource_profiles(program, compiled, rows)
    last = max((row.end_index for row in rows), default=0)
    out: list[ResourceKPI] = []
    for resource in program.resources:
        available = compiled.availability[resource.id][:last] or [resource.capacity_units]
        used = load[resource.id][:last] or [0]
        peak = max((u / a for u, a in zip(used, available, strict=False) if a > 0), default=0.0)
        capacity_total = sum(available)
        mean = sum(used) / capacity_total if capacity_total else 0.0
        over = sum(1 for u, a in zip(used, available, strict=False) if u > a)
        out.append(
            ResourceKPI(
                resource_id=resource.id,
                peak_pct=round(100 * peak, 1),
                mean_pct=round(100 * mean, 1),
                overload_days=over,
            )
        )
    return out
