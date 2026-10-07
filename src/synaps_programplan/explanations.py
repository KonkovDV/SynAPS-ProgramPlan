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
from synaps_programplan.evidence import fingerprint
from synaps_programplan.model import OKRProgram, TaskStatus, required_changeover
from synaps_programplan.planner import Adjustments, SolveConfig, adjustments_of, plan, plan_hash
from synaps_programplan.result import CauseKind, Claim, Explanation, ExplanationFact, PlanResult, TaskPlan

_WINDOW_TEXT = {
    "status_date": "не может начаться раньше даты статуса программы",
    "earliest_start": "ограничение «не раньше» в исходном плане",
    "shift_limit": "достигнут допустимый лимит сдвига",
    "pinned": "работа закреплена (pinned)",
    "frozen": "работа в зоне заморозки плана",
    "in_progress": "работа уже выполняется",
    "scenario": (
        "ограничение варианта: сценарий или ручная правка (закрепление, «не раньше исходного варианта»)"
    ),
}


@dataclass(frozen=True)
class _Cause:
    code: str
    refs: list[str]
    text: str
    kind: CauseKind
    resource_id: str | None = None
    blocker_task_id: str | None = None
    dates: tuple[str, ...] = ()


_WINDOW_KIND = {
    "status_date": CauseKind.STATUS_DATE,
    "earliest_start": CauseKind.EARLIEST_START,
    "shift_limit": CauseKind.BASELINE,
    "pinned": CauseKind.BASELINE,
    "frozen": CauseKind.FREEZE_WINDOW,
    "in_progress": CauseKind.IN_PROGRESS,
    "scenario": CauseKind.BASELINE,
}

_RESERVED: frozenset[CauseKind] = frozenset()


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
    articles: set[str] = field(default_factory=set)
    cache: dict[str, _Cause] = field(default_factory=dict)


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
    if adjustments is None:
        adjustments = adjustments_of(result)
    ctx = _context(program, result, adjustments)
    late = {m.task_id for m in (result.kpi.milestones if result.kpi else []) if m.lateness_wd > 0}
    modes = {task.id for task in program.tasks if task.modes}
    targets = [
        row
        for row in result.tasks
        if row.task_id in ctx.compiled.windows
        and (row.shift_wd or row.task_id in late or row.task_id in modes)
    ]
    targets.sort(key=lambda r: (-abs(r.shift_wd or 0), r.task_id))
    out: list[Explanation] = []
    stamped_plan = str(result.evidence.get("plan_hash") or plan_hash(result))
    stamped_input = str(result.evidence.get("input_hash") or fingerprint(program))
    for row in targets[: limit or len(targets)]:
        cause = _cause(ctx, row.task_id)
        chain = _chain(ctx, row.task_id)
        shift = row.shift_wd or 0
        head = f"«{row.name}»"
        if shift > 0:
            head += f" сдвинута на {shift} раб. дн. позже"
        elif shift < 0:
            head += f" перенесена на {-shift} раб. дн. раньше"
        root = ""
        if len(chain) > 1:
            root_cause = _cause(ctx, chain[-1])
            if root_cause.code not in ("DEPENDENCY", "CROSS_PROJECT_DEPENDENCY", "CYCLE_GUARD"):
                root = (
                    f"; первопричина через {len(chain) - 1} зв.: «{ctx.names[chain[-1]]}» — {root_cause.text}"
                )
        dates = [row.start.isoformat(), *cause.dates]
        seen: list[str] = []
        for value in dates:
            if value not in seen:
                seen.append(value)
        blocker = cause.blocker_task_id
        task = program.task(row.task_id)
        chosen = next((mode for mode in task.modes if mode.code == row.mode_code), None)
        if chosen is not None:
            mode_text = f"выбран режим {chosen.code}, {chosen.duration_wd} раб. дн."
            text = f"{head}: {mode_text}" + (f". {cause.text}{root}" if shift else "")
            kind = CauseKind.MODE_SELECTION
            code = CauseKind.MODE_SELECTION.value
        else:
            text = f"{head}: {cause.text}{root}"
            kind = cause.kind
            code = cause.code
        out.append(
            Explanation(
                task_id=row.task_id,
                shift_wd=shift,
                cause_code=code,
                cause_refs=cause.refs,
                chain=chain,
                text=text,
                fact=ExplanationFact(
                    kind=kind,
                    resource_id=cause.resource_id,
                    project_id=ctx.projects.get(blocker, row.project_id) if blocker else row.project_id,
                    blocker_task_id=blocker,
                    dates=seen,
                    plan_hash=stamped_plan,
                    input_hash=stamped_input,
                    mode_code=row.mode_code if chosen is not None else None,
                ),
            )
        )
    return out


def explanation_gaps(program: OKRProgram, result: PlanResult) -> list[str]:
    """Stored explanations must equal a fresh reading of the same accepted plan."""
    if not result.outcome.ok:
        return []
    stamped = result.evidence.get("input_hash")
    if stamped is not None and stamped != fingerprint(program):
        return ["хеш программы в плане не совпадает с программой, по которой читается причина"]
    fresh = {item.task_id: item for item in explain(program, result)}
    gaps: list[str] = []
    for item in result.explanations:
        again = fresh.get(item.task_id)
        if again is None:
            gaps.append(f"{item.task_id}: в плане нет причины для этой записи")
            continue
        same = (
            item.cause_code == again.cause_code
            and item.cause_refs == again.cause_refs
            and item.chain == again.chain
            and item.shift_wd == again.shift_wd
            and item.text == again.text
        )
        if item.fact is not None and item.fact != again.fact:
            same = False
        if not same:
            gaps.append(f"{item.task_id}: текст не совпадает с фактами плана")
    return gaps


def fact_errors(program: OKRProgram, result: PlanResult) -> list[str]:
    """Each stored fact must name the plan's own start date, hashes and blocker.

    A file written before typed facts is not failed here: this checks facts
    that are present, and a fresh ``explain`` always writes one.
    """
    if not result.outcome.ok:
        return []
    live_input = fingerprint(program)
    live_plan = plan_hash(result)
    rows = {row.task_id: row for row in result.tasks}
    projects = {project.id for project in program.projects}
    resources = {resource.id for resource in program.resources}
    incoming: dict[str, set[str]] = defaultdict(set)
    for edge in program.dependencies:
        incoming[edge.dst_task_id].add(edge.src_task_id)
    errors: list[str] = []
    covered: set[str] = set()
    for item in result.explanations:
        fact = item.fact
        if fact is None:
            errors.append(f"{item.task_id}: у причины нет факта")
            continue
        covered.add(item.task_id)
        if fact.kind in _RESERVED:
            errors.append(
                f"{item.task_id}: тип {fact.kind.value} зарезервирован и в этом плане не проверяется"
            )
        if fact.input_hash != live_input:
            errors.append(f"{item.task_id}: хеш входа в факте не совпадает с программой")
        if fact.plan_hash != live_plan and fact.plan_hash != str(result.evidence.get("plan_hash") or ""):
            errors.append(f"{item.task_id}: хеш плана в факте не совпадает с планом")
        row = rows.get(item.task_id)
        if row is None:
            errors.append(f"{item.task_id}: работы нет в плане")
            continue
        if fact.kind is CauseKind.MODE_SELECTION:
            named = program.task(item.task_id)
            codes = {mode.code for mode in named.modes}
            if fact.mode_code not in codes or fact.mode_code != row.mode_code:
                errors.append(f"{item.task_id}: режим {fact.mode_code!r} не выбран в этом плане")
        allowed = {row.start.isoformat(), row.finish.isoformat()}
        if fact.blocker_task_id:
            blocker = rows.get(fact.blocker_task_id)
            if blocker is None:
                errors.append(f"{item.task_id}: блокер {fact.blocker_task_id} отсутствует в плане")
            else:
                allowed.add(blocker.start.isoformat())
                allowed.add(blocker.finish.isoformat())
        if row.start.isoformat() not in fact.dates:
            errors.append(f"{item.task_id}: в факте нет даты начала {row.start.isoformat()}")
        for value in fact.dates:
            if value not in allowed:
                errors.append(f"{item.task_id}: дата {value} не относится к работе или блокеру")
        if fact.project_id is not None and fact.project_id not in projects:
            errors.append(f"{item.task_id}: ОКР {fact.project_id} нет в программе")
        if fact.resource_id is not None and fact.resource_id not in resources:
            errors.append(f"{item.task_id}: ресурса {fact.resource_id} нет в программе")
        if fact.kind in (
            CauseKind.PRECEDENCE,
            CauseKind.MAX_LAG,
        ) and fact.blocker_task_id not in incoming.get(item.task_id, set()):
            errors.append(f"{item.task_id}: блокер не является предшественником")
        if (
            fact.kind
            in (
                CauseKind.RESOURCE_CAPACITY,
                CauseKind.SKILL_CAPACITY,
                CauseKind.CALENDAR,
                CauseKind.MAINTENANCE,
                CauseKind.TEST_ARTICLE,
                CauseKind.SETUP_TRANSITION,
            )
            and not fact.resource_id
        ):
            errors.append(f"{item.task_id}: у факта о ресурсе нет ресурса")
    fresh = {item.task_id for item in explain(program, result)}
    for task_id in fresh:
        if task_id not in covered and task_id in rows and (rows[task_id].shift_wd or rows[task_id].mode_code):
            errors.append(f"{task_id}: сдвиг без факта")
    return errors


def _context(program: OKRProgram, result: PlanResult, adjustments: Adjustments) -> _Ctx:
    compiled = compile_program(
        program,
        extra_lo=adjustments.extra_lo,
        extra_hi=adjustments.extra_hi,
        capacity_scale=adjustments.capacity_scale,
        due_override=adjustments.due_override,
        ignore_due_projects=adjustments.ignore_due_projects,
    )
    horizon = len(compiled.axis)
    rows = {row.task_id: row for row in result.tasks}
    tasks = {task.id: task for task in program.tasks}
    load = {resource.id: [0] * horizon for resource in program.resources}
    users: dict[str, list[TaskPlan]] = defaultdict(list)
    for row in result.tasks:
        if row.status is TaskStatus.DONE:
            continue
        for demand in tasks[row.task_id].demands_for(row.mode_code):
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
        articles={article.resource_id for article in program.articles},
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


def _window_cause(reason: str) -> _Cause:
    return _Cause(
        code=f"WINDOW_{reason.upper()}",
        refs=[],
        text=_WINDOW_TEXT.get(reason, reason),
        kind=_WINDOW_KIND.get(reason, CauseKind.OPTIMIZER_CHOICE),
    )


def _max_lag_holds(ctx: _Ctx, task_id: str, src: str, start: int) -> bool:
    duration = ctx.compiled.windows[task_id].duration
    src_row = ctx.rows[src]
    for edge in ctx.program.dependencies:
        if edge.dst_task_id != task_id or edge.src_task_id != src or not edge.hard or edge.max_lag_wd is None:
            continue
        if src_row.status is TaskStatus.DONE:
            anchor = ctx.compiled.fixed_anchor[src]
            value = (anchor[1] if anchor_is_end_src(edge.type) else anchor[0]) or 0
        else:
            value = src_row.end_index if anchor_is_end_src(edge.type) else src_row.start_index
        bound = value + edge.max_lag_wd - (duration if anchor_is_end_dst(edge.type) else 0)
        if bound == start:
            return True
    return False


def _cause(ctx: _Ctx, task_id: str) -> _Cause:
    if task_id in ctx.cache:
        return ctx.cache[task_id]
    ctx.cache[task_id] = _Cause("CYCLE_GUARD", [], "", CauseKind.PRECEDENCE)
    row = ctx.rows[task_id]
    window = ctx.compiled.windows[task_id]
    start = row.start_index
    tight = [(src, rel) for bound, src, rel in _edge_bounds(ctx, task_id) if bound == start]
    if window.fixed:
        answer = _window_cause(window.reasons_lo[-1])
    elif tight:
        src, rel = sorted(tight)[0]
        cross = ctx.projects[src] != ctx.projects[task_id]
        answer = _Cause(
            code="CROSS_PROJECT_DEPENDENCY" if cross else "DEPENDENCY",
            refs=[src],
            text=(
                f"ждёт «{ctx.names[src]}» (связь {rel}"
                + (f", {_project_code(ctx, ctx.projects[src])}" if cross else "")
                + ")"
            ),
            kind=CauseKind.MAX_LAG if _max_lag_holds(ctx, task_id, src, start) else CauseKind.PRECEDENCE,
            blocker_task_id=src,
            dates=(ctx.rows[src].start.isoformat(),),
        )
    elif start == window.lo:
        reason = window.reasons_lo[-1]
        if reason.startswith("dep:"):
            src = reason[4:]
            answer = _Cause(
                code="DEPENDENCY_DONE",
                refs=[src],
                text=f"ждёт выполненную/начатую работу «{ctx.names.get(src, src)}»",
                kind=CauseKind.PRECEDENCE,
                blocker_task_id=src,
            )
        else:
            answer = _window_cause(reason)
    else:
        setup = _changeover_cause(ctx, row)
        answer = setup if setup is not None else _resource_cause(ctx, row)
    ctx.cache[task_id] = answer
    return answer


def _changeover_cause(ctx: _Ctx, row: TaskPlan) -> _Cause | None:
    task = ctx.program.task(row.task_id)
    if not task.stand_state_id or not ctx.program.changeovers:
        return None
    states = {item.id: item for item in ctx.program.stand_states}
    named = states.get(task.stand_state_id)
    if named is None:
        return None
    start = row.start_index
    best: tuple[int, str] | None = None
    for other in ctx.rows.values():
        if other.task_id == row.task_id or other.end_index > start:
            continue
        previous = ctx.program.task(other.task_id)
        if previous.stand_state_id is None:
            continue
        previous_state = states.get(previous.stand_state_id)
        if previous_state is None or previous_state.resource_id != named.resource_id:
            continue
        required = required_changeover(
            ctx.program, named.resource_id, previous.stand_state_id, task.stand_state_id
        )
        if required <= 0 or other.end_index + required != start:
            continue
        if best is None or other.end_index > best[0]:
            best = (other.end_index, other.task_id)
    if best is None:
        return None
    previous_id = best[1]
    previous = ctx.program.task(previous_id)
    required = required_changeover(
        ctx.program, named.resource_id, previous.stand_state_id or "", task.stand_state_id
    )
    return _Cause(
        code=CauseKind.SETUP_TRANSITION.value,
        refs=[previous_id],
        text=(
            f"переналадка стенда {named.resource_id} с {previous.stand_state_id} "
            f"на {task.stand_state_id}, {required} раб. дн."
        ),
        kind=CauseKind.SETUP_TRANSITION,
        resource_id=named.resource_id,
        blocker_task_id=previous_id,
        dates=(ctx.rows[previous_id].finish.isoformat(),),
    )


def _project_code(ctx: _Ctx, project_id: str) -> str:
    for project in ctx.program.projects:
        if project.id == project_id:
            return project.code
    return project_id


def _closure_kind(ctx: _Ctx, resource_id: str, day: int, skill: bool) -> CauseKind:
    if resource_id in ctx.articles:
        return CauseKind.TEST_ARTICLE
    if skill:
        return CauseKind.SKILL_CAPACITY
    day_date = ctx.compiled.axis.days[day]
    for item in ctx.program.capacity_exceptions:
        if (
            item.resource_id == resource_id
            and item.start <= day_date <= item.end
            and item.reason.value == "MAINTENANCE"
        ):
            return CauseKind.MAINTENANCE
    return CauseKind.CALENDAR


def _load_kind(ctx: _Ctx, resource_id: str, skill: bool) -> CauseKind:
    if resource_id in ctx.articles:
        return CauseKind.TEST_ARTICLE
    return CauseKind.SKILL_CAPACITY if skill else CauseKind.RESOURCE_CAPACITY


def _resource_cause(ctx: _Ctx, row: TaskPlan) -> _Cause:
    task = ctx.program.task(row.task_id)
    probe = row.start_index - 1
    last = min(probe + row.duration_wd, len(ctx.compiled.axis))
    reserve: _Cause | None = None
    for demand in task.demands_for(row.mode_code):
        rid = demand.resource_id or row.bound.get(demand.skill_id or "")
        if rid is None or rid not in ctx.avail:
            continue
        skill = demand.skill_id is not None
        for day in range(max(probe, 0), last):
            own = demand.units if row.start_index <= day < row.end_index else 0
            if ctx.load[rid][day] - own + demand.units <= ctx.avail[rid][day]:
                continue
            resource = next(item for item in ctx.program.resources if item.id == rid)
            occupants = [
                other.task_id
                for other in ctx.users[rid]
                if other.task_id != row.task_id and other.start_index <= day < other.end_index
            ]
            when = f"{ctx.compiled.axis.days[day]:%d.%m.%Y}"
            raw = ctx.compiled.availability[rid][day]
            if not occupants and raw > 0:
                if reserve is None:
                    reserve = _Cause(
                        code="CAPACITY_RESERVE",
                        refs=[rid],
                        text=(
                            f"{resource.code}: резерв варианта оставил {ctx.avail[rid][day]} из {raw} ед., "
                            f"задаче нужно {demand.units} ({when})"
                        ),
                        kind=_load_kind(ctx, rid, skill),
                        resource_id=rid,
                    )
                continue
            if not occupants:
                return _Cause(
                    code="CALENDAR_CLOSURE",
                    refs=[rid],
                    text=f"{resource.code} недоступен {when} (отпуск/ТО/календарь ресурса)",
                    kind=_closure_kind(ctx, rid, day, skill),
                    resource_id=rid,
                )
            others = sorted({ctx.projects[item] for item in occupants} - {row.project_id})
            names = ", ".join(f"«{ctx.names[item]}»" for item in occupants[:3])
            foreign = ", ".join(_project_code(ctx, item) for item in others)
            blocker = occupants[0]
            return _Cause(
                code="RESOURCE_CONTENTION" if others else "RESOURCE_BUSY",
                refs=[rid, *occupants],
                text=f"{resource.code} занят {when}: {names}" + (f" ({foreign})" if foreign else ""),
                kind=_load_kind(ctx, rid, skill),
                resource_id=rid,
                blocker_task_id=blocker,
                dates=(ctx.rows[blocker].start.isoformat(),),
            )
    return reserve or _Cause(
        code="OPTIMIZER_CHOICE",
        refs=[],
        text="положение выбрано оптимизатором (ограничение не активно)",
        kind=CauseKind.OPTIMIZER_CHOICE,
    )


def _chain(ctx: _Ctx, task_id: str, depth: int = 40) -> list[str]:
    chain = [task_id]
    current = task_id
    for _ in range(depth):
        cause = _cause(ctx, current)
        if (
            cause.code not in ("DEPENDENCY", "CROSS_PROJECT_DEPENDENCY")
            or not cause.refs
            or cause.refs[0] in chain
        ):
            break
        current = cause.refs[0]
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
        "fact_kind": CauseKind.COUNTERFACTUAL.value,
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


_LATEST_SUFFIX = "#latest_finish"


def relaxable_requirements(program: OKRProgram) -> list[Requirement]:
    out: list[Requirement] = []
    for task in program.tasks:
        if task.status is TaskStatus.DONE:
            continue
        # Each bound is its own requirement: relaxing one must not relax the other.
        if task.deadline is not None:
            out.append(
                Requirement("deadline", task.id, f"директивный срок «{task.name}» {task.deadline:%d.%m.%Y}")
            )
        if task.latest_finish is not None:
            out.append(
                Requirement(
                    "deadline",
                    f"{task.id}{_LATEST_SUFFIX}",
                    f"окончание не позже {task.latest_finish:%d.%m.%Y} у «{task.name}»",
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
            task["deadline"] = None
        if f"{task['id']}{_LATEST_SUFFIX}" in by_kind["deadline"]:
            task["latest_finish"] = None
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
            "fact_kind": CauseKind.INFEASIBILITY_CONFLICT.value,
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
        "fact_kind": CauseKind.INFEASIBILITY_CONFLICT.value,
        "witness": witness,
        "minimal": minimal,
        "probes": probes,
        "text": _witness_text(witness),
    }


def _witness_text(witness: list[dict[str, Any]]) -> str:
    text = "несовместимы требования: " + "; ".join(
        str(item.get("text") or item.get("ref")) for item in witness
    )
    if len(witness) < 2 or any("alone_sufficient" not in item for item in witness):
        return text
    flags = [item.get("alone_sufficient") for item in witness]
    if all(flag is True for flag in flags):
        return text + ". Достаточно ослабить любое из них"
    if all(flag is False for flag in flags):
        return text + ". Ослабления одного недостаточно: нужно ослабить весь набор"
    return text


def _needed_relaxation(program: OKRProgram, item: Requirement, config: SolveConfig) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": item.kind, "ref": item.ref, "text": item.text}
    if item.kind not in ("deadline", "project_deadline"):
        return out
    relaxed = plan(relax(program, [item]), SolveConfig(time_limit_s=config.time_limit_s, objective="due"))
    if not relaxed.outcome.ok:
        out["alone_sufficient"] = False if relaxed.outcome.claim.value == "INFEASIBLE" else None
        return out
    if item.kind == "deadline":
        task_id = item.ref.removesuffix(_LATEST_SUFFIX)
        finish: date = relaxed.task(task_id).finish
        bound = program.task(task_id)
        target = bound.latest_finish if item.ref.endswith(_LATEST_SUFFIX) else bound.deadline
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
