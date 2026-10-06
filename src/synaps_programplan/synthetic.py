"""Seeded generator of synthetic R&D (OKR) programs.

Stages come from ``okr_stages.json`` (technical assignment, preliminary and
technical design, working documentation, prototype, preliminary tests,
state and certification tests). Source-plan dates are an unlevelled CPM schedule,
i.e. what a project office gets when per-project MS Project files are merged
without resource levelling - so shared specialists and stands are overloaded
on purpose.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

from synaps_programplan.calendar import WorkCalendar, WorkdayAxis
from synaps_programplan.cpm import cpm
from synaps_programplan.model import (
    FTE_UNITS,
    Baseline,
    BaselineDates,
    Calendar,
    CalendarBase,
    CapacityException,
    Demand,
    Dependency,
    DependencySource,
    DependencyType,
    ExceptionReason,
    OKRProgram,
    Program,
    Project,
    Provenance,
    ProvenanceKind,
    Resource,
    ResourceKind,
    RiskDriver,
    Skill,
    Task,
    TaskKind,
    WBSKind,
    WBSNode,
    difference_constraints,
)
from synaps_programplan.stages import stage_catalog

SKILLS = {
    "designer": "Инженер-конструктор",
    "electronics": "Инженер-схемотехник",
    "software": "Инженер-программист",
    "systems": "Системный инженер",
    "technologist": "Инженер-технолог",
    "tester": "Инженер-испытатель",
}


@dataclass(frozen=True)
class SyntheticSpec:
    projects: int = 4
    tasks_per_stage: int = 3
    people_per_skill: int = 2
    stands: int = 2
    cross_project_share: float = 0.25
    ss_ff_share: float = 0.15
    deadline_slack: float = 1.4
    vacations: int = 4
    maintenance: int = 1
    enterprises: tuple[str, ...] = ()
    seed: int = 7
    start: date = date(2026, 10, 5)
    horizon_end: date = date(2029, 12, 28)
    infeasible: bool = False


def generate(spec: SyntheticSpec | None = None) -> OKRProgram:
    spec = spec or SyntheticSpec()
    rng = random.Random(spec.seed)
    calendar = Calendar(id="ru", base=CalendarBase.RU_PRODUCTION)
    skills = [Skill(id=key, code=key, name=name) for key, name in SKILLS.items()]
    resources: list[Resource] = []
    for key in SKILLS:
        for index in range(spec.people_per_skill):
            resources.append(
                Resource(
                    id=f"p-{key}-{index + 1}",
                    kind=ResourceKind.PERSON,
                    code=f"{key.upper()}-{index + 1}",
                    name=f"{SKILLS[key]} {index + 1}",
                    capacity_units=FTE_UNITS,
                    skills=[key],
                    org_unit=f"Отдел {key}",
                )
            )
    for index in range(spec.stands):
        resources.append(
            Resource(
                id=f"stand-{index + 1}",
                kind=ResourceKind.STAND,
                code=f"TS-{index + 1}",
                name=f"Испытательный стенд ТС-{index + 1}",
                capacity_units=1,
            )
        )
    projects: list[Project] = []
    wbs: list[WBSNode] = []
    tasks: list[Task] = []
    deps: list[Dependency] = []
    stage_milestones: dict[str, list[str]] = {}
    stages = stage_catalog()
    for p_index in range(spec.projects):
        project_id = f"okr{p_index + 1}"
        enterprise = spec.enterprises[p_index % len(spec.enterprises)] if spec.enterprises else None
        projects.append(
            Project(
                id=project_id,
                code=f"ОКР-{p_index + 1}",
                name=f"ОКР «Изделие-{p_index + 1}»",
                priority=rng.choice([300, 500, 700]),
                enterprise=enterprise,
            )
        )
        previous_ms: str | None = None
        stage_milestones[project_id] = []
        for s_index, stage in enumerate(stages):
            code, title, stage_skills = stage.code, stage.name, list(stage.skills)
            node_id = f"{project_id}.{code}"
            wbs.append(
                WBSNode(
                    id=node_id, project_id=project_id, code=f"{s_index + 1}", name=title, kind=WBSKind.STAGE
                )
            )
            stage_tasks: list[str] = []
            for t_index in range(spec.tasks_per_stage):
                task_id = f"{node_id}.{t_index + 1}"
                is_test = stage.test
                skill = rng.choice(stage_skills)
                demands = [Demand(skill_id=skill, units=rng.choice([5, 10, 10]))]
                if is_test:
                    demands.append(Demand(resource_id=f"stand-{rng.randint(1, spec.stands)}", units=1))
                tasks.append(
                    Task(
                        id=task_id,
                        project_id=project_id,
                        wbs_id=node_id,
                        name=f"{title}: работа {t_index + 1}",
                        duration_wd=rng.randint(8, 30),
                        kind=TaskKind.TEST if is_test else TaskKind.WORK,
                        demands=demands,
                        okr_stage=code,
                    )
                )
                if stage_tasks and rng.random() < 0.6:
                    kind = DependencyType.FS
                    lag = 0
                    if rng.random() < spec.ss_ff_share:
                        kind = rng.choice([DependencyType.SS, DependencyType.FF])
                        lag = rng.randint(2, 6)
                    deps.append(
                        Dependency(src_task_id=stage_tasks[-1], dst_task_id=task_id, type=kind, lag_wd=lag)
                    )
                elif previous_ms is not None:
                    deps.append(Dependency(src_task_id=previous_ms, dst_task_id=task_id))
                stage_tasks.append(task_id)
            ms_id = f"{node_id}.M"
            tasks.append(
                Task(
                    id=ms_id,
                    project_id=project_id,
                    wbs_id=node_id,
                    name=f"Веха: {title} — завершение",
                    duration_wd=0,
                    kind=TaskKind.MILESTONE,
                    okr_stage=code,
                )
            )
            for task_id in stage_tasks:
                deps.append(Dependency(src_task_id=task_id, dst_task_id=ms_id))
            if previous_ms is not None and stage_tasks:
                deps.append(Dependency(src_task_id=previous_ms, dst_task_id=stage_tasks[0]))
            previous_ms = ms_id
            stage_milestones[project_id].append(ms_id)
    _cross_links(rng, spec, projects, stage_milestones, tasks, deps)
    program = Program(
        id="prog",
        name="Учебная программа ОКР",
        calendar_id="ru",
        horizon_start=spec.start,
        horizon_end=spec.horizon_end,
        status_date=spec.start,
    )
    draft = OKRProgram(
        program=program,
        calendars=[calendar],
        projects=projects,
        wbs=wbs,
        tasks=tasks,
        dependencies=_dedupe(deps),
        resources=resources,
        skills=skills,
    )
    return _with_dates(rng, spec, draft)


def _cross_links(
    rng: random.Random,
    spec: SyntheticSpec,
    projects: list[Project],
    stage_milestones: dict[str, list[str]],
    tasks: list[Task],
    deps: list[Dependency],
) -> None:
    ids = [p.id for p in projects]
    first_tasks = {t.wbs_id: t.id for t in reversed(tasks) if t.kind is not TaskKind.MILESTONE}
    for dst_project in ids[1:]:
        if rng.random() > spec.cross_project_share * 2:
            continue
        src_project = rng.choice([p for p in ids if p < dst_project])
        src_stage = rng.randint(1, 3)
        dst_stage = rng.randint(src_stage, 4)
        src_ms = stage_milestones[src_project][src_stage]
        dst_task = first_tasks[stage_milestones[dst_project][dst_stage].rsplit(".", 1)[0]]
        deps.append(
            Dependency(
                src_task_id=src_ms,
                dst_task_id=dst_task,
                lag_wd=rng.randint(0, 5),
                source=DependencySource.CROSS_PROJECT,
            )
        )


def _dedupe(deps: list[Dependency]) -> list[Dependency]:
    seen: set[tuple[str, str, DependencyType]] = set()
    out: list[Dependency] = []
    for dep in deps:
        key = (dep.src_task_id, dep.dst_task_id, dep.type)
        if key not in seen:
            seen.add(key)
            out.append(dep)
    return out


def _with_dates(rng: random.Random, spec: SyntheticSpec, draft: OKRProgram) -> OKRProgram:
    """Approved plan = every OKR levelled on its own (as separate MS Project files are).

    Conflicts appear only when the projects share people and stands, which is
    the situation the consolidated plan has to resolve.
    """
    calendar = WorkCalendar.from_model(draft.calendars[0])
    axis = WorkdayAxis.build(calendar, spec.start, spec.horizon_end)
    exceptions = _exceptions(rng, spec, draft, axis)
    positions = _levelled_per_project(draft, exceptions)
    tasks: list[Task] = []
    baseline: dict[str, BaselineDates] = {}
    last_stage = stage_catalog()[-1].code
    finals = {p.id: f"{p.id}.{last_stage}.M" for p in draft.projects}
    for task in draft.tasks:
        start_idx, end_idx = positions[task.id]
        start = axis.event_date(end_idx) if task.duration_wd == 0 else axis.start_date(start_idx)
        finish = axis.finish_date(start_idx, end_idx)
        update: dict[str, object] = {"planned_start": start, "planned_finish": finish}
        if task.id in finals.values():
            slack = spec.deadline_slack if not spec.infeasible else 1.05
            update["deadline"] = axis.event_date(min(len(axis) - 1, int(end_idx * slack)))
        elif task.duration_wd == 0 and rng.random() < 0.3:
            update["due_date"] = axis.event_date(min(len(axis) - 1, int(end_idx * 1.05) + 5))
        tasks.append(task.model_copy(update=update))
        baseline[task.id] = BaselineDates(start=start, finish=finish)
    updated = draft.model_copy(
        update={
            "tasks": tasks,
            "capacity_exceptions": exceptions,
            "baseline": Baseline(id="bl-0", approved_by="synthetic", task_dates=baseline),
            "risk_drivers": _risk_drivers(draft),
            "provenance": Provenance(kind=ProvenanceKind.SYNTHETIC, source=f"synthetic seed={spec.seed}"),
        }
    )
    return OKRProgram.model_validate(updated.model_dump())


def _risk_drivers(draft: OKRProgram) -> list[RiskDriver]:
    """Typical OKR risk register entries; illustrative values, not customer data."""
    by_stage: dict[str, list[str]] = {}
    for task in draft.tasks:
        if task.duration_wd > 0 and task.okr_stage:
            by_stage.setdefault(task.okr_stage, []).append(task.id)
    specs = [
        ("R-TEST", "Повторные испытания после отказа на стенде", 0.35, 1.1, 1.4, 1.9, ["PI"]),
        ("R-SUPPLY", "Задержка поставки комплектующих опытного образца", 0.3, 1.0, 1.25, 1.7, ["OO"]),
        ("R-DOC", "Доработка КД по замечаниям нормоконтроля и заказчика", 0.4, 1.0, 1.15, 1.4, ["RKD"]),
        ("R-CERT", "Замечания государственных и сертификационных испытаний", 0.25, 1.0, 1.2, 1.6, ["GI"]),
    ]
    drivers: list[RiskDriver] = []
    for driver_id, name, probability, low, mode, high, stages in specs:
        task_ids = [task_id for stage in stages for task_id in by_stage.get(stage, [])]
        if task_ids:
            drivers.append(
                RiskDriver(
                    id=driver_id,
                    name=name,
                    probability=probability,
                    low=low,
                    mode=mode,
                    high=high,
                    task_ids=task_ids,
                    owner="учебный реестр",
                )
            )
    return drivers


def _levelled_per_project(
    draft: OKRProgram, exceptions: list[CapacityException]
) -> dict[str, tuple[int, int]]:
    from synaps_programplan.planner import SolveConfig, plan  # local: planner does not depend on synthetic

    durations = {task.id: task.duration_wd for task in draft.tasks}
    lower = {task.id: (1 if task.duration_wd == 0 else 0) for task in draft.tasks}
    unlevelled = cpm(durations, difference_constraints(draft), lower)
    positions = {
        task_id: (start, start + durations[task_id]) for task_id, start in unlevelled.early_start.items()
    }
    for project in draft.projects:
        own = {task.id for task in draft.tasks if task.project_id == project.id}
        sub = draft.model_copy(
            update={
                "projects": [project],
                "wbs": [w for w in draft.wbs if w.project_id == project.id],
                "tasks": [t for t in draft.tasks if t.id in own],
                "dependencies": [
                    d for d in draft.dependencies if d.src_task_id in own and d.dst_task_id in own
                ],
                "capacity_exceptions": exceptions,
            }
        )
        result = plan(OKRProgram.model_validate(sub.model_dump()), SolveConfig(solver="greedy"))
        if result.outcome.ok:
            positions.update({row.task_id: (row.start_index, row.end_index) for row in result.tasks})
    return positions


def _exceptions(
    rng: random.Random, spec: SyntheticSpec, draft: OKRProgram, axis: WorkdayAxis
) -> list[CapacityException]:
    people = [r for r in draft.resources if r.kind is ResourceKind.PERSON]
    stands = [r for r in draft.resources if r.kind is ResourceKind.STAND]
    out: list[CapacityException] = []
    for _ in range(spec.vacations):
        person = rng.choice(people)
        start = rng.randint(20, 200)
        out.append(
            CapacityException(
                resource_id=person.id,
                start=axis.days[start],
                end=axis.days[start + 9],
                units_available=0,
                reason=ExceptionReason.VACATION,
            )
        )
    for _ in range(spec.maintenance if stands else 0):
        stand = rng.choice(stands)
        start = rng.randint(60, 200)
        out.append(
            CapacityException(
                resource_id=stand.id,
                start=axis.days[start],
                end=axis.days[start + 4],
                units_available=0,
                reason=ExceptionReason.MAINTENANCE,
            )
        )
    return out
