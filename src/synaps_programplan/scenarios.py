"""Alternative plans with their impact (requirement F4).

Until the kernel returns solution sets, alternatives are produced by an outer
loop over scenario profiles (each one a different SynAPS solve):

A "Сроки"        - minimise completion of every OKR and milestone lateness.
B "Ресурсы"      - same, but shared people/stands keep a capacity reserve.
C "Стабильность" - minimum total delay against the approved plan, nothing
                   earlier than approved (fallback: smallest uniform shift limit).
D "Баланс"       - program finish within (1+eps) of A, then minimum delay.
P "Приоритет"    - lexicographic by OKR priority: OKRs of the top level are
                   planned first, their finish and milestone dates become
                   upper bounds, then the next level is added (only when the
                   projects carry at least two priority levels).
E "Что если"     - A on a modified program (extra capacity, moved milestone,
                   dropped OKR, delayed task).

Plans with identical task dates are reported once (``duplicate_of``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date

from synaps_programplan.compiler import compile_program, reference_index
from synaps_programplan.model import OKRProgram, TaskStatus
from synaps_programplan.planner import Adjustments, SolveConfig, plan
from synaps_programplan.result import PlanResult

SHIFT_LADDER = (0, 2, 5, 10, 20, 40, 80, 160)


@dataclass
class WhatIf:
    label: str = "Что если"
    add_capacity: dict[str, int] = field(default_factory=dict)
    move_deadline: dict[str, date] = field(default_factory=dict)
    drop_projects: list[str] = field(default_factory=list)
    delay_task_wd: dict[str, int] = field(default_factory=dict)


@dataclass
class ScenarioSet:
    plans: list[PlanResult]
    duplicates: dict[str, str]

    def by_id(self, scenario_id: str) -> PlanResult:
        return next(p for p in self.plans if p.scenario_id == scenario_id)


def shared_resources(program: OKRProgram) -> list[str]:
    users: dict[str, set[str]] = defaultdict(set)
    members = {r.id: r for r in program.resources}
    for task in program.tasks:
        for demand in task.demands:
            if demand.resource_id is not None:
                users[demand.resource_id].add(task.project_id)
            elif demand.skill_id is not None:
                for rid, resource in members.items():
                    if demand.skill_id in resource.skills:
                        users[rid].add(task.project_id)
    return sorted(rid for rid, projects in users.items() if len(projects) > 1)


def _windows_for_shift(program: OKRProgram, delta: int, cap: int | None) -> Adjustments:
    compiled = compile_program(program)
    refs = reference_index(compiled, program)
    adjustments = Adjustments()
    for task_id, window in compiled.windows.items():
        if cap is not None:
            adjustments.extra_hi[task_id] = cap
        if task_id not in refs or window.fixed:
            continue
        ref = refs[task_id]
        adjustments.extra_lo[task_id] = ref - delta
        hi = ref + delta + window.duration
        adjustments.extra_hi[task_id] = min(hi, cap) if cap is not None else hi
    return adjustments


def _smallest_shift(
    program: OKRProgram,
    config: SolveConfig,
    *,
    scenario_id: str,
    label: str,
    cap: int | None,
    warm: PlanResult | None,
) -> PlanResult | None:
    probe = SolveConfig(**{**config.__dict__, "time_limit_s": max(3, config.time_limit_s // 2)})
    for delta in SHIFT_LADDER:
        result = plan(
            program,
            probe,
            scenario_id=scenario_id,
            label=label,
            adjustments=_windows_for_shift(program, delta, cap),
            warm_start=warm,
        )
        if result.outcome.ok:
            result.metadata["shift_limit_wd"] = delta
            return result
    return None


def apply_what_if(program: OKRProgram, what_if: WhatIf) -> OKRProgram:
    data = program.model_dump()
    dropped = set(what_if.drop_projects)
    if dropped:
        keep = {t["id"] for t in data["tasks"] if t["project_id"] not in dropped}
        data["projects"] = [p for p in data["projects"] if p["id"] not in dropped]
        data["wbs"] = [w for w in data["wbs"] if w["project_id"] not in dropped]
        data["tasks"] = [t for t in data["tasks"] if t["id"] in keep]
        data["dependencies"] = [
            d for d in data["dependencies"] if d["src_task_id"] in keep and d["dst_task_id"] in keep
        ]
        if data.get("baseline"):
            data["baseline"]["task_dates"] = {
                k: v for k, v in data["baseline"]["task_dates"].items() if k in keep
            }
        drivers = []
        for driver in data.get("risk_drivers", []):
            driver["task_ids"] = [t for t in driver["task_ids"] if t in keep]
            if driver["task_ids"]:
                drivers.append(driver)
        data["risk_drivers"] = drivers
    for resource in data["resources"]:
        resource["capacity_units"] += what_if.add_capacity.get(resource["id"], 0)
    for task in data["tasks"]:
        if task["id"] in what_if.move_deadline:
            task["deadline"] = what_if.move_deadline[task["id"]]
            task["latest_finish"] = None
        if task["id"] in what_if.delay_task_wd and task["status"] == TaskStatus.PLANNED:
            task["duration_wd"] += what_if.delay_task_wd[task["id"]]
    return OKRProgram.model_validate(data)


def run_scenarios(
    program: OKRProgram,
    config: SolveConfig | None = None,
    *,
    what_ifs: list[WhatIf] | None = None,
    epsilon: float = 0.05,
    reserve: float = 0.8,
) -> ScenarioSet:
    config = config or SolveConfig()
    plans: list[PlanResult] = []
    base = plan(program, config, scenario_id="A", label="A · Сроки")
    plans.append(base)

    shared = shared_resources(program)
    resource_plan: PlanResult | None = None
    for scale in (reserve, (1 + reserve) / 2):
        resource_plan = plan(
            program,
            config,
            scenario_id="B",
            label=f"B · Ресурсы (резерв {round(100 * (1 - scale))}% на общих ресурсах)",
            adjustments=Adjustments(capacity_scale=dict.fromkeys(shared, scale)),
            warm_start=base,
        )
        resource_plan.metadata["capacity_reserve"] = round(1 - scale, 3)
        if resource_plan.outcome.ok:
            break
    if resource_plan is not None:
        plans.append(resource_plan)

    if program.reference_dates():
        stable_config = replace(config, objective="stability")
        stable = plan(
            program,
            stable_config,
            scenario_id="C",
            label="C · Стабильность (мин. отклонений)",
            warm_start=base,
        )
        if not stable.outcome.ok:
            fallback = _smallest_shift(
                program, config, scenario_id="C", label="C · Стабильность", cap=None, warm=base
            )
            if fallback is not None:
                fallback.label = f"C · Стабильность (сдвиг ≤ {fallback.metadata['shift_limit_wd']} раб. дн.)"
                stable = fallback
        plans.append(stable)
        if base.outcome.ok and base.tasks:
            finish = max(row.end_index for row in base.tasks if row.task_id in _active(program))
            cap = int(finish * (1 + epsilon)) + 1
            balanced = plan(
                program,
                stable_config,
                scenario_id="D",
                label=f"D · Баланс (срок ≤ A+{round(100 * epsilon)}%, мин. отклонений)",
                adjustments=Adjustments(extra_hi=dict.fromkeys(_active(program), cap)),
                warm_start=base,
            )
            balanced.metadata["finish_cap_index"] = cap
            plans.append(balanced)

    prioritised = priority_plan(program, config, warm=base)
    if prioritised is not None:
        plans.append(prioritised)

    for index, what_if in enumerate(what_ifs or []):
        modified = apply_what_if(program, what_if)
        result = plan(
            modified,
            config,
            scenario_id=f"E{index + 1}",
            label=f"E{index + 1} · {what_if.label}",
            warm_start=base,
        )
        result.metadata["what_if"] = {
            "add_capacity": what_if.add_capacity,
            "move_deadline": {k: v.isoformat() for k, v in what_if.move_deadline.items()},
            "drop_projects": what_if.drop_projects,
            "delay_task_wd": what_if.delay_task_wd,
        }
        plans.append(result)
    return ScenarioSet(plans=plans, duplicates=_dedupe(plans))


def priority_plan(
    program: OKRProgram, config: SolveConfig, *, warm: PlanResult | None = None
) -> PlanResult | None:
    """Scenario P: higher-priority OKRs never lose time to lower-priority ones.

    Level by level (highest ``Project.priority`` first) the kernel optimises
    only the OKRs planned so far - lower levels are scheduled, but their soft
    due dates are ignored. The milestones and terminal tasks of those OKRs
    then get the dates they reached as upper bounds, and the next level is
    added. Each step starts from the previous plan, which already meets every
    bound, so the bounds never make the next step infeasible. ``None`` when the
    program has a single priority level.
    """
    levels = sorted({p.priority for p in program.projects if _project_active(program, p.id)}, reverse=True)
    if len(levels) < 2:
        return None
    targets = _targets(program)
    caps: dict[str, int] = {}
    steps: list[dict[str, object]] = []
    previous = warm if warm is not None and warm.outcome.ok else None
    result: PlanResult | None = None
    for level in levels:
        ignored = frozenset(p.id for p in program.projects if p.priority < level)
        result = plan(
            program,
            config,
            scenario_id="P",
            label="P · Приоритет ОКР",
            adjustments=Adjustments(extra_hi=dict(caps), ignore_due_projects=ignored),
            warm_start=previous,
        )
        steps.append(
            {
                "priority": level,
                "claim": result.outcome.claim.value,
                "ok": result.outcome.ok,
                "bounds": len(caps),
            }
        )
        if not result.outcome.ok:
            break
        planned = {p.id for p in program.projects if p.priority >= level}
        for row in result.tasks:
            if row.task_id in targets and row.project_id in planned:
                caps[row.task_id] = min(caps.get(row.task_id, row.end_index), row.end_index)
        previous = result
    assert result is not None
    result.metadata["priority_levels"] = steps
    result.metadata["priority_bounds"] = len(caps)
    return result


def _targets(program: OKRProgram) -> set[str]:
    """Tasks whose dates carry an OKR's result: milestones, dated tasks, terminal tasks."""
    has_successor = {d.src_task_id for d in program.dependencies if d.hard}
    return {
        t.id
        for t in program.tasks
        if t.status is not TaskStatus.DONE
        and (
            t.duration_wd == 0
            or t.due_date is not None
            or t.deadline is not None
            or t.latest_finish is not None
            or t.id not in has_successor
        )
    }


def _project_active(program: OKRProgram, project_id: str) -> bool:
    return any(t.project_id == project_id and t.status is not TaskStatus.DONE for t in program.tasks)


def _active(program: OKRProgram) -> set[str]:
    return {t.id for t in program.tasks if t.status is not TaskStatus.DONE}


def _dedupe(plans: list[PlanResult]) -> dict[str, str]:
    seen: dict[tuple[tuple[str, int], ...], str] = {}
    duplicates: dict[str, str] = {}
    for result in plans:
        if not result.outcome.ok:
            continue
        key = tuple(sorted((row.task_id, row.start_index) for row in result.tasks))
        if key in seen:
            duplicates[result.scenario_id] = seen[key]
            result.metadata["duplicate_of"] = seen[key]
        else:
            seen[key] = result.scenario_id
    return duplicates


def compare(plans: list[PlanResult]) -> list[dict[str, object]]:
    """One KPI row per scenario (input of the comparison table and the UI)."""
    rows: list[dict[str, object]] = []
    for result in plans:
        kpi = result.kpi
        row: dict[str, object] = {
            "scenario": result.scenario_id,
            "label": result.label,
            "ok": result.outcome.ok,
            "claim": result.outcome.claim.value,
            "duplicate_of": result.metadata.get("duplicate_of"),
        }
        if kpi is not None and result.outcome.ok:
            peak = max((r.peak_pct for r in kpi.resources), default=0.0)
            row.update(
                {
                    "program_finish": kpi.program_finish.isoformat() if kpi.program_finish else None,
                    "late_milestones": kpi.late_count,
                    "tardiness_wd": kpi.tardiness_wd,
                    "moved_tasks": kpi.moved_count,
                    "shift_sum_wd": kpi.shift_sum_wd,
                    "shift_max_wd": kpi.shift_max_wd,
                    "peak_load_pct": peak,
                    "project_finish": {k: v.isoformat() for k, v in kpi.project_finish.items()},
                }
            )
        rows.append(row)
    return rows
