"""Manual edits of an accepted plan (requirement F5: drag a task, see the check).

``apply_moves`` puts the moved tasks on new start dates (snapped to working
days) and leaves every other date untouched. ``check_moves`` runs the
independent domain checker on the result - no solver is involved, so a manual
plan is judged by exactly the rules an optimised one is. ``repair_with_moves``
keeps the moved tasks where the user put them and re-solves everything else,
by default with as few changes to the accepted plan as possible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from typing import Any, Literal

from synaps_programplan.checker import check_plan
from synaps_programplan.compiler import Compiled, compile_program, reference_index
from synaps_programplan.explanations import explain
from synaps_programplan.model import OKRProgram, ResourceKind, TaskStatus
from synaps_programplan.planner import Adjustments, SolveConfig, _mark_critical, compute_kpi, plan
from synaps_programplan.result import PlanResult, Severity, TaskPlan

ReplanMode = Literal["stable", "optimise"]


def _move_row(
    compiled: Compiled, refs: dict[str, int], row: TaskPlan, new_start: date, status_date: date
) -> TaskPlan:
    """Row on ``new_start``. A date the working-day axis cannot hold is refused, never moved silently."""
    axis = compiled.axis
    if new_start < status_date:
        raise ValueError(
            f"{row.task_id}: дата {new_start.isoformat()} раньше даты статуса {status_date.isoformat()}"
        )
    last_day = axis.days[-1]
    if row.is_milestone:
        if new_start > last_day:
            raise ValueError(f"{row.task_id}: дата {new_start.isoformat()} позже горизонта программы")
        boundary = max(1, axis.boundary_after(new_start))
        start_index = end_index = boundary
        start = finish = axis.event_date(boundary)
    else:
        start_index = axis.index_on_or_after(new_start)
        if start_index >= len(axis):
            raise ValueError(f"{row.task_id}: дата {new_start.isoformat()} позже горизонта программы")
        end_index = start_index + row.duration_wd
        if end_index > len(axis):
            raise ValueError(
                f"{row.task_id}: работа не помещается в горизонт программы "
                f"(последний рабочий день {last_day.isoformat()})"
            )
        start = axis.start_date(start_index)
        finish = axis.finish_date(start_index, end_index)
    shift = start_index - refs[row.task_id] if row.task_id in refs else None
    return row.model_copy(
        update={
            "start": start,
            "finish": finish,
            "start_index": start_index,
            "end_index": end_index,
            "shift_wd": shift,
        }
    )


def apply_moves(program: OKRProgram, base: PlanResult, moves: dict[str, date]) -> list[TaskPlan]:
    if not base.outcome.ok:
        raise ValueError("only an accepted plan (outcome.ok) can be edited")
    known = {row.task_id for row in base.tasks}
    unknown = sorted(set(moves) - known)
    if unknown:
        raise ValueError(f"unknown tasks in moves: {unknown[:5]}")
    compiled = compile_program(program)
    refs = reference_index(compiled, program)
    rows = [
        _move_row(compiled, refs, row, moves[row.task_id], program.program.planning_start)
        if row.task_id in moves
        else row.model_copy()
        for row in base.tasks
    ]
    _mark_critical(program, compiled, rows)
    return rows


def check_moves(program: OKRProgram, base: PlanResult, moves: dict[str, date]) -> dict[str, Any]:
    rows = apply_moves(program, base, moves)
    violations = check_plan(program, rows)
    hard = [v for v in violations if v.severity is Severity.HARD]
    kpi = compute_kpi(program, compile_program(program), rows)
    moved = {row.task_id: row for row in rows if row.task_id in moves}
    return {
        "ok": not hard,
        "hard": len(hard),
        "violations": [v.model_dump(mode="json") for v in violations],
        "kpi": kpi.model_dump(mode="json"),
        "moved": [
            {
                "task_id": t,
                "start": r.start.isoformat(),
                "finish": r.finish.isoformat(),
                "shift_wd": r.shift_wd,
            }
            for t, r in moved.items()
        ],
    }


def repair_with_moves(
    program: OKRProgram,
    base: PlanResult,
    moves: dict[str, date],
    config: SolveConfig | None = None,
    *,
    scenario_id: str = "edit",
    label: str = "Правка планировщика",
    mode: ReplanMode = "stable",
) -> PlanResult:
    """Pin the moved tasks at the requested dates and re-solve the rest.

    ``stable`` (default) keeps every other task no earlier than in ``base`` and
    minimises the total delay against ``base``: an edit disturbs as little of
    the accepted plan as it can. If that has no accepted plan, the run falls
    back to ``optimise`` and says so in ``metadata.replan``. ``optimise``
    re-solves the rest with the configured objective.
    """
    if mode not in ("stable", "optimise"):
        raise ValueError("mode must be 'stable' or 'optimise'")
    tasks = {task.id: task for task in program.tasks}
    frozen = sorted(t for t in moves if tasks[t].status is not TaskStatus.PLANNED)
    if frozen:
        raise ValueError(f"started or finished work cannot be moved: {frozen[:5]}")
    rows = {row.task_id: row for row in apply_moves(program, base, moves)}
    pins_lo = {t: rows[t].start_index for t in moves}
    pins_hi = {t: rows[t].end_index for t in moves}
    config = config or SolveConfig()
    info: dict[str, Any] = {"base": base.scenario_id, "pinned": sorted(moves), "mode": mode}
    result: PlanResult | None = None
    if mode == "stable":
        adjustments = _stable_adjustments(program, base, pins_lo, pins_hi)
        result = plan(
            program,
            replace(config, objective="stability"),
            scenario_id=scenario_id,
            label=label,
            adjustments=adjustments,
            warm_start=base,
        )
        if not result.outcome.ok:
            info = {**info, "mode": "optimise", "stable_claim": result.outcome.claim.value}
            result = None
    if result is None:
        adjustments = Adjustments(extra_lo=pins_lo, extra_hi=pins_hi)
        result = plan(
            program,
            config,
            scenario_id=scenario_id,
            label=label,
            adjustments=adjustments,
            warm_start=base,
        )
    if result.outcome.ok and info["mode"] == "stable":
        info["churn"] = classify_churn(program, base, result, set(moves))
    result.metadata["replan"] = info
    if result.outcome.ok:
        result.explanations = explain(program, result, adjustments=adjustments)
    return result


def classify_churn(
    program: OKRProgram, base: PlanResult, result: PlanResult, pinned: set[str]
) -> dict[str, Any]:
    """Split moved tasks by what the edit can actually force.

    The affected set grows until it stops: hard-link successors of an affected
    task, then every task that shares a resource or a skill with the set.
    ``other`` is outside that set. A stable replan has no reason to move it,
    and the pilot limit applies to this group, not to the successor chain.
    """
    base_start = {row.task_id: row.start_index for row in base.tasks}
    moved = {row.task_id for row in result.tasks if base_start.get(row.task_id) != row.start_index}
    downstream, resource = _affected(program, pinned)
    downstream -= pinned
    resource -= pinned | downstream
    other = sorted(moved - pinned - downstream - resource)
    span = max(base_start.values(), default=0)
    weighted = 0
    frozen_moved = 0
    freeze_until = program.freeze.freeze_until
    for row in result.tasks:
        old = base_start.get(row.task_id)
        if old is None or old == row.start_index:
            continue
        weight = 1 + max(0, span - min(old, row.start_index))
        weighted += abs(row.start_index - old) * weight
        if (
            freeze_until is not None
            and row.reference_start is not None
            and row.reference_start < freeze_until
        ):
            frozen_moved += 1
    return {
        "tasks": len(result.tasks),
        "moved": len(moved),
        "pinned": len(pinned & moved),
        "downstream": len(moved & downstream),
        "resource": len(moved & resource),
        "other": len(other),
        "other_ids": other[:20],
        "weighted_shift_wd": weighted,
        "stand_order_changes": _stand_order_changes(program, base, result),
        "frozen_moved": frozen_moved,
    }


def _affected(program: OKRProgram, seeds: set[str]) -> tuple[set[str], set[str]]:
    succ: dict[str, list[str]] = defaultdict(list)
    for edge in program.dependencies:
        if edge.hard:
            succ[edge.src_task_id].append(edge.dst_task_id)
    uses = _resource_keys(program)
    affected = set(seeds)
    downstream: set[str] = set()
    resource: set[str] = set()
    growing = True
    while growing:
        growing = False
        stack = list(affected)
        seen = set(affected)
        while stack:
            node = stack.pop()
            for nxt in succ.get(node, []):
                if nxt not in seen:
                    seen.add(nxt)
                    downstream.add(nxt)
                    stack.append(nxt)
                    growing = True
        affected |= seen
        hot: set[str] = set()
        for task_id in affected:
            hot.update(uses.get(task_id, ()))
        for task_id, keys in uses.items():
            if task_id not in affected and hot.intersection(keys):
                affected.add(task_id)
                resource.add(task_id)
                growing = True
    return downstream, resource


def _stand_order_changes(program: OKRProgram, base: PlanResult, result: PlanResult) -> int:
    """How many stands have a different task order after the replan."""
    stands = [resource.id for resource in program.resources if resource.kind is ResourceKind.STAND]
    users: dict[str, set[str]] = {stand: set() for stand in stands}
    for task in program.tasks:
        for demand in task.demands:
            if demand.resource_id in users:
                users[demand.resource_id].add(task.id)
    changes = 0
    for task_ids in users.values():
        if _order(base, task_ids) != _order(result, task_ids):
            changes += 1
    return changes


def _order(plan: PlanResult, task_ids: set[str]) -> tuple[str, ...]:
    rows = [row for row in plan.tasks if row.task_id in task_ids]
    rows.sort(key=lambda row: (row.start_index, row.task_id))
    return tuple(row.task_id for row in rows)


def _resource_keys(program: OKRProgram) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for task in program.tasks:
        keys = [
            f"resource:{demand.resource_id}" if demand.resource_id else f"skill:{demand.skill_id}"
            for demand in task.demands
            if demand.resource_id or demand.skill_id
        ]
        out[task.id] = tuple(keys)
    return out


def _stable_adjustments(
    program: OKRProgram, base: PlanResult, pins_lo: dict[str, int], pins_hi: dict[str, int]
) -> Adjustments:
    """No task earlier than in ``base``; soft due = its ``base`` end."""
    windows = compile_program(program).windows
    extra_lo = dict(pins_lo)
    due: dict[str, int] = {}
    for row in base.tasks:
        window = windows.get(row.task_id)
        if row.task_id in pins_lo or window is None or window.fixed:
            continue
        if window.lo <= row.start_index and row.end_index <= window.hi:
            extra_lo[row.task_id] = row.start_index
        due[row.task_id] = row.end_index
    return Adjustments(extra_lo=extra_lo, extra_hi=dict(pins_hi), due_override=due)
