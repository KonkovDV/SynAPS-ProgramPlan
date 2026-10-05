"""Manual edits of an accepted plan (requirement F5: drag a task, see the check).

``apply_moves`` puts the moved tasks on new start dates (snapped to working
days) and leaves every other date untouched. ``check_moves`` runs the
independent domain checker on the result - no solver is involved, so a manual
plan is judged by exactly the rules an optimised one is. ``repair_with_moves``
keeps the moved tasks where the user put them and re-solves everything else,
by default with as few changes to the accepted plan as possible.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, Literal

from synaps_programplan.checker import check_plan
from synaps_programplan.compiler import Compiled, compile_program, reference_index
from synaps_programplan.explanations import explain
from synaps_programplan.model import OKRProgram, TaskStatus
from synaps_programplan.planner import Adjustments, SolveConfig, _mark_critical, compute_kpi, plan
from synaps_programplan.result import PlanResult, Severity, TaskPlan

ReplanMode = Literal["stable", "optimise"]


def _move_row(compiled: Compiled, refs: dict[str, int], row: TaskPlan, new_start: date) -> TaskPlan:
    axis = compiled.axis
    if row.is_milestone:
        boundary = max(1, axis.boundary_after(new_start))
        start_index = end_index = boundary
        start = finish = axis.event_date(boundary)
    else:
        start_index = min(axis.index_on_or_after(new_start), len(axis) - 1)
        end_index = start_index + row.duration_wd
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
        _move_row(compiled, refs, row, moves[row.task_id]) if row.task_id in moves else row.model_copy()
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
    result.metadata["replan"] = info
    if result.outcome.ok:
        result.explanations = explain(program, result, adjustments=adjustments)
    return result


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
