"""Why was a task moved (requirement F5) and why is a program infeasible.

Explanations are computed from the FINAL verified plan, never invented:

* binding cause of every start: window bound (status date, earliest start,
  shift limit, pinned, scenario), a tight dependency, or a resource that is
  full the day before (with its occupants and their OKR projects), or a
  calendar closure (vacation, maintenance);
* a causal chain that follows tight dependencies up to the root cause;
* counterfactuals: re-solve with the task kept at its approved dates;
* infeasibility witness: a deletion-minimal set of relaxable hard requirements
  (deadlines, pins, freeze, shift limits, capacity exceptions, max lags) that
  together make the program infeasible, each with the relaxation it needs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from synaps_programplan.compiler import Compiled, anchor_is_end_dst, anchor_is_end_src, compile_program
from synaps_programplan.model import OKRProgram, TaskStatus
from synaps_programplan.planner import Adjustments, SolveConfig, plan
from synaps_programplan.result import Claim, Explanation, PlanResult, TaskPlan

_WINDOW_TEXT = {
    "status_date": "не может начаться раньше даты статуса программы",
    "earliest_start": "ограничение «не раньше» в исходном плане",
    "shift_limit": "достигнут допустимый лимит сдвига",
    "pinned": "работа закреплена (pinned)",
    "frozen": "работа в зоне заморозки плана",
    "in_progress": "работа уже выполняется",
    "scenario": "ограничение сценария",
}


@dataclass
class _Ctx:
    program: OKRProgram
    compiled: Compiled
    rows: dict[str, TaskPlan]
    load: dict[str, list[int]]
    avail: dict[str, list[int]]
    users: dict[str, list[TaskPlan]]
    names: dict[str, str]
    projects: dict[str, str]
    cache: dict[str, tuple[str, list[str], str]] = field(default_factory=dict)


def explain(
    program: OKRProgram,
    result: PlanResult,
    *,
    adjustments: Adjustments | None = None,
    limit: int | None = None,
) -> list[Explanation]:
    """Explanations for every moved task and every late milestone of an accepted plan."""
    if not result.outcome.ok:
        return []
    adjustments = adjustments or Adjustments()
    ctx = _context(program, result, adjustments)
    late = {m.task_id for m in (result.kpi.milestones if result.kpi else []) if m.lateness_wd > 0}
    targets = [
        row
        for row in result.tasks
        if row.task_id in ctx.compiled.windows and (row.shift_wd or row.task_id in late)
    ]
    targets.sort(key=lambda r: (-abs(r.shift_wd or 0), r.task_id))
    out: list[Explanation] = []
    for row in targets[: limit or len(targets)]:
        code, refs, text = _cause(ctx, row.task_id)
        chain = _chain(ctx, row.task_id)
        shift = row.shift_wd or 0
        head = f"«{row.name}»"
        if shift > 0:
            head += f" сдвинута на {shift} раб. дн. позже"
        elif shift < 0:
            head += f" перенесена на {-shift} раб. дн. раньше"
        root = ""
        if len(chain) > 1:
            root_code, _, root_text = _cause(ctx, chain[-1])
            if root_code not in ("DEPENDENCY", "CROSS_PROJECT_DEPENDENCY", "CYCLE_GUARD"):
                root = f"; первопричина через {len(chain) - 1} зв.: «{ctx.names[chain[-1]]}» — {root_text}"
        out.append(
            Explanation(
                task_id=row.task_id,
                shift_wd=shift,
                cause_code=code,
                cause_refs=refs,
                chain=chain,
                text=f"{head}: {text}{root}",
            )
        )
    return out


def _context(program: OKRProgram, result: PlanResult, adjustments: Adjustments) -> _Ctx:
    compiled = compile_program(
        program,
        extra_lo=adjustments.extra_lo,
        extra_hi=adjustments.extra_hi,
        capacity_scale=adjustments.capacity_scale,
    )
    horizon = len(compiled.axis)
    rows = {row.task_id: row for row in result.tasks}
    tasks = {task.id: task for task in program.tasks}
    load = {resource.id: [0] * horizon for resource in program.resources}
    users: dict[str, list[TaskPlan]] = defaultdict(list)
    for row in result.tasks:
        if row.status is TaskStatus.DONE:
            continue
        for demand in tasks[row.task_id].demands:
            rid = demand.resource_id or row.bound.get(demand.skill_id or "")
            if rid is None:
                continue
            users[rid].append(row)
            for day in range(max(0, row.start_index), min(horizon, row.end_index)):
                load[rid][day] += demand.units
    avail: dict[str, list[int]] = {}
    for resource in program.resources:
        cap = max(1, int(resource.capacity_units * adjustments.capacity_scale.get(resource.id, 1.0)))
        avail[resource.id] = [min(cap, units) for units in compiled.availability[resource.id]]
    return _Ctx(
        program=program,
        compiled=compiled,
        rows=rows,
        load=load,
        avail=avail,
        users=users,
        names={task.id: task.name for task in program.tasks},
        projects={task.id: task.project_id for task in program.tasks},
    )


def _edge_bounds(ctx: _Ctx, task_id: str) -> list[tuple[int, str, str]]:
    """(bound on start, source task, relation text) for incoming hard dependencies."""
    out: list[tuple[int, str, str]] = []
    duration = ctx.compiled.windows[task_id].duration
    for edge in ctx.program.dependencies:
        if edge.dst_task_id != task_id or not edge.hard or edge.src_task_id not in ctx.rows:
            continue
        src = ctx.rows[edge.src_task_id]
        if src.status is TaskStatus.DONE:
            anchor = ctx.compiled.fixed_anchor[edge.src_task_id]
            value = (anchor[1] if anchor_is_end_src(edge.type) else anchor[0]) or 0
        else:
            value = src.end_index if anchor_is_end_src(edge.type) else src.start_index
        bound = value + edge.lag_wd - (duration if anchor_is_end_dst(edge.type) else 0)
        lag = f"{edge.lag_wd:+d}" if edge.lag_wd else ""
        out.append((bound, edge.src_task_id, f"{edge.type.value}{lag}"))
    return out


def _cause(ctx: _Ctx, task_id: str) -> tuple[str, list[str], str]:
    if task_id in ctx.cache:
        return ctx.cache[task_id]
    ctx.cache[task_id] = ("CYCLE_GUARD", [], "")
    row = ctx.rows[task_id]
    window = ctx.compiled.windows[task_id]
    start = row.start_index
    answer: tuple[str, list[str], str]
    tight = [(src, rel) for bound, src, rel in _edge_bounds(ctx, task_id) if bound == start]
    if window.fixed:
        reason = window.reasons_lo[-1]
        answer = (f"WINDOW_{reason.upper()}", [], _WINDOW_TEXT.get(reason, reason))
    elif tight:
        src, rel = sorted(tight)[0]
        cross = ctx.projects[src] != ctx.projects[task_id]
        answer = (
            "CROSS_PROJECT_DEPENDENCY" if cross else "DEPENDENCY",
            [src],
            f"ждёт «{ctx.names[src]}» (связь {rel}" + (f", ОКР {ctx.projects[src]}" if cross else "") + ")",
        )
    elif start == window.lo:
        reason = window.reasons_lo[-1]
        if reason.startswith("dep:"):
            src = reason[4:]
            answer = (
                "DEPENDENCY_DONE",
                [src],
                f"ждёт выполненную/начатую работу «{ctx.names.get(src, src)}»",
            )
        else:
            answer = (f"WINDOW_{reason.upper()}", [], _WINDOW_TEXT.get(reason, reason))
    else:
        answer = _resource_cause(ctx, row)
    ctx.cache[task_id] = answer
    return answer


def _resource_cause(ctx: _Ctx, row: TaskPlan) -> tuple[str, list[str], str]:
    task = ctx.program.task(row.task_id)
    probe = row.start_index - 1
    for demand in task.demands:
        rid = demand.resource_id or row.bound.get(demand.skill_id or "")
        if rid is None:
            continue
        for day in range(probe, min(probe + row.duration_wd, len(ctx.compiled.axis))):
            own = demand.units if row.start_index <= day < row.end_index else 0
            if ctx.load[rid][day] - own + demand.units <= ctx.avail[rid][day]:
                continue
            resource = next(r for r in ctx.program.resources if r.id == rid)
            occupants = [
                u.task_id
                for u in ctx.users[rid]
                if u.task_id != row.task_id and u.start_index <= day < u.end_index
            ]
            when = f"{ctx.compiled.axis.days[day]:%d.%m.%Y}"
            if not occupants:
                return (
                    "CALENDAR_CLOSURE",
                    [rid],
                    f"{resource.code} недоступен {when} (отпуск/ТО/календарь ресурса)",
                )
            others = sorted({ctx.projects[t] for t in occupants} - {row.project_id})
            names = ", ".join(f"«{ctx.names[t]}»" for t in occupants[:3])
            return (
                "RESOURCE_CONTENTION" if others else "RESOURCE_BUSY",
                [rid, *occupants],
                f"{resource.code} занят {when}: {names}" + (f" (ОКР {', '.join(others)})" if others else ""),
            )
    return ("OPTIMIZER_CHOICE", [], "положение выбрано оптимизатором (ограничение не активно)")


def _chain(ctx: _Ctx, task_id: str, depth: int = 40) -> list[str]:
    chain = [task_id]
    current = task_id
    for _ in range(depth):
        code, refs, _ = _cause(ctx, current)
        if code not in ("DEPENDENCY", "CROSS_PROJECT_DEPENDENCY") or not refs or refs[0] in chain:
            break
        current = refs[0]
        chain.append(current)
    return chain


# ---------- counterfactuals ----------


def counterfactual(
    program: OKRProgram, result: PlanResult, task_id: str, config: SolveConfig | None = None
) -> dict[str, Any]:
    """Re-solve with ``task_id`` kept at its approved start; report KPI deltas."""
    config = config or SolveConfig(time_limit_s=10)
    row = result.task(task_id)
    compiled = compile_program(program)
    if row.shift_wd is None or task_id not in compiled.windows:
        return {"task_id": task_id, "applicable": False}
    ref = row.start_index - row.shift_wd
    keep = Adjustments(extra_lo={task_id: ref}, extra_hi={task_id: ref + compiled.windows[task_id].duration})
    alt = plan(program, config, scenario_id=f"cf:{task_id}", label="контрфакт", adjustments=keep)
    out: dict[str, Any] = {
        "task_id": task_id,
        "applicable": True,
        "feasible": alt.outcome.ok,
        "claim": alt.outcome.claim.value,
    }
    if alt.outcome.ok and alt.kpi and result.kpi and alt.kpi.program_finish and result.kpi.program_finish:
        axis = compiled.axis
        out["program_finish_delta_wd"] = axis.boundary_after(alt.kpi.program_finish) - axis.boundary_after(
            result.kpi.program_finish
        )
        out["late_milestones_delta"] = alt.kpi.late_count - result.kpi.late_count
        out["tardiness_delta_wd"] = alt.kpi.tardiness_wd - result.kpi.tardiness_wd
        moved = sorted(
            (r.task_id for r in alt.tasks if r.start_index != result.task(r.task_id).start_index),
        )
        out["other_tasks_moved"] = len([t for t in moved if t != task_id])
        out["text"] = (
            f"Если оставить «{row.name}» на утверждённых датах: срок программы "
            f"{out['program_finish_delta_wd']:+d} раб. дн., "
            f"просроченных вех {out['late_milestones_delta']:+d}, "
            f"сдвинется других работ: {out['other_tasks_moved']}"
        )
    elif alt.outcome.claim.value == "INFEASIBLE":
        out["text"] = f"Оставить «{row.name}» на утверждённых датах нельзя: доказано, что план невыполним"
    else:
        out["text"] = (
            f"Плана с «{row.name}» на утверждённых датах не найдено за {config.time_limit_s} с "
            "(невыполнимость не доказана)"
        )
    return out


def attach_counterfactuals(
    program: OKRProgram, result: PlanResult, *, top: int = 3, config: SolveConfig | None = None
) -> None:
    for item in result.explanations[:top]:
        if item.shift_wd > 0:
            item.counterfactual = counterfactual(program, result, item.task_id, config)


# ---------- infeasibility witness ----------


@dataclass(frozen=True)
class Requirement:
    kind: str
    ref: str
    text: str


def relaxable_requirements(program: OKRProgram) -> list[Requirement]:
    out: list[Requirement] = []
    for task in program.tasks:
        if task.status is TaskStatus.DONE:
            continue
        if task.hard_finish is not None:
            out.append(
                Requirement(
                    "deadline", task.id, f"директивный срок «{task.name}» {task.hard_finish:%d.%m.%Y}"
                )
            )
        if task.pinned:
            out.append(Requirement("pinned", task.id, f"закрепление «{task.name}»"))
        if task.shift_limit_wd is not None:
            out.append(
                Requirement("shift_limit", task.id, f"лимит сдвига «{task.name}» {task.shift_limit_wd} р.д.")
            )
        if task.earliest_start is not None:
            out.append(
                Requirement(
                    "earliest_start", task.id, f"«не раньше» {task.earliest_start:%d.%m.%Y} у «{task.name}»"
                )
            )
    for project in program.projects:
        if project.deadline is not None:
            out.append(
                Requirement(
                    "project_deadline", project.id, f"срок ОКР {project.code} {project.deadline:%d.%m.%Y}"
                )
            )
    if program.freeze.freeze_until is not None:
        out.append(
            Requirement("freeze", "freeze", f"заморозка плана до {program.freeze.freeze_until:%d.%m.%Y}")
        )
    for index, row in enumerate(program.capacity_exceptions):
        out.append(
            Requirement(
                "capacity_exception",
                str(index),
                f"недоступность {row.resource_id} {row.start:%d.%m}–{row.end:%d.%m.%Y} ({row.reason})",
            )
        )
    for edge in program.dependencies:
        if edge.hard and edge.max_lag_wd is not None:
            out.append(
                Requirement(
                    "max_lag",
                    f"{edge.src_task_id}>{edge.dst_task_id}",
                    f"макс. лаг {edge.max_lag_wd} р.д. {edge.src_task_id}→{edge.dst_task_id}",
                )
            )
    return out


def relax(program: OKRProgram, dropped: list[Requirement]) -> OKRProgram:
    data = program.model_dump()
    by_kind: dict[str, set[str]] = defaultdict(set)
    for item in dropped:
        by_kind[item.kind].add(item.ref)
    for task in data["tasks"]:
        if task["id"] in by_kind["deadline"]:
            task["deadline"] = task["latest_finish"] = None
        if task["id"] in by_kind["pinned"]:
            task["pinned"] = False
        if task["id"] in by_kind["shift_limit"]:
            task["shift_limit_wd"] = None
        if task["id"] in by_kind["earliest_start"]:
            task["earliest_start"] = None
    for project in data["projects"]:
        if project["id"] in by_kind["project_deadline"]:
            project["deadline"] = None
    if by_kind["freeze"]:
        data["freeze"]["freeze_until"] = None
    data["capacity_exceptions"] = [
        row
        for index, row in enumerate(data["capacity_exceptions"])
        if str(index) not in by_kind["capacity_exception"]
    ]
    for edge in data["dependencies"]:
        if f"{edge['src_task_id']}>{edge['dst_task_id']}" in by_kind["max_lag"]:
            edge["max_lag_wd"] = None
    return OKRProgram.model_validate(data)


def _status(program: OKRProgram, config: SolveConfig) -> str:
    """``infeasible`` needs a CP-SAT proof on the pooled model (a relaxation of
    named staffing); ``feasible`` needs a fully verified plan."""
    outcome = plan(program, config).outcome
    if outcome.ok:
        return "feasible"
    return "infeasible" if outcome.claim is Claim.INFEASIBLE else "unknown"


def infeasibility_witness(
    program: OKRProgram, *, time_limit_s: int = 8, max_probes: int = 200
) -> dict[str, Any]:
    """Deletion filter (chunked) over relaxable hard requirements.

    Every kept requirement is necessary for infeasibility under the probe
    budget: removing it alone from the witness makes the program feasible or
    unknown. ``unknown`` probes keep the requirement and mark the witness
    ``minimal=False``.
    """
    config = SolveConfig(time_limit_s=time_limit_s, objective="due")
    requirements = relaxable_requirements(program)
    if _status(program, config) != "infeasible":
        return {"infeasible": False}
    if _status(relax(program, requirements), config) == "infeasible":
        return {
            "infeasible": True,
            "witness": [],
            "minimal": True,
            "text": "программа невыполнима даже без директивных сроков и закреплений: "
            "не хватает горизонта планирования или ресурсов (резерв ресурса < требования работы)",
        }
    kept = list(requirements)
    probes, minimal = 0, True
    index, chunk = 0, max(1, len(kept) // 2)
    while index < len(kept) and probes < max_probes:
        rest = kept[:index] + kept[index + chunk :]
        probes += 1
        status = _status(relax(program, [r for r in requirements if r not in rest]), config)
        if status == "infeasible":
            kept = rest
            chunk = max(1, min(chunk, len(kept) - index))
        elif chunk > 1:
            chunk //= 2
        else:
            minimal = minimal and status == "feasible"
            index += 1
    if index < len(kept):
        minimal = False
    witness = [_needed_relaxation(program, item, config) for item in kept[:5]] + [
        {"kind": item.kind, "ref": item.ref, "text": item.text} for item in kept[5:]
    ]
    return {
        "infeasible": True,
        "witness": witness,
        "minimal": minimal,
        "probes": probes,
        "text": "несовместимы требования: " + "; ".join(item.text for item in kept),
    }


def _needed_relaxation(program: OKRProgram, item: Requirement, config: SolveConfig) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": item.kind, "ref": item.ref, "text": item.text}
    if item.kind not in ("deadline", "project_deadline"):
        return out
    relaxed = plan(relax(program, [item]), SolveConfig(time_limit_s=config.time_limit_s, objective="due"))
    if not relaxed.outcome.ok:
        out["alone_sufficient"] = False if relaxed.outcome.claim.value == "INFEASIBLE" else None
        return out
    if item.kind == "deadline":
        finish: date = relaxed.task(item.ref).finish
        target = program.task(item.ref).hard_finish
    else:
        finish = max(r.finish for r in relaxed.tasks if r.project_id == item.ref)
        target = next(p.deadline for p in program.projects if p.id == item.ref)
    out["alone_sufficient"] = True
    out["achievable_date"] = finish.isoformat()
    if target is not None:
        axis = compile_program(program).axis
        out["relax_by_wd"] = max(0, axis.boundary_after(finish) - axis.boundary_after(target))
        out["suggestion"] = f"перенести срок на {finish:%d.%m.%Y} (+{out['relax_by_wd']} раб. дн.)"
    return out
