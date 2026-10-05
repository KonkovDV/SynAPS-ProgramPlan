"""MS Project MPP and MPX import through MPXJ.

MPP is a proprietary binary format, so it is read by MPXJ (LGPL, needs Java 17)
rather than by a parser of our own. The same reader accepts MPX. Mapping matches
the MSPDI import: summary tasks become WBS nodes, leaf tasks become tasks,
FS/SS/FF/SF links keep their lag in working days, work resources become people.
Everything skipped or approximated is listed in the import report.

The dependency is optional (``pip install SynAPS-ProgramPlan[mpp]``). Without it
``read_mpp`` raises ``ValueError`` and names what to install.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from synaps_programplan.io.mspdi import ImportedProject, ImportReport
from synaps_programplan.model import (
    FTE_UNITS,
    Demand,
    Dependency,
    DependencyType,
    Resource,
    ResourceKind,
    Task,
    TaskKind,
    TaskStatus,
    WBSKind,
    WBSNode,
)

_MINUTES = {
    "m": 1.0,
    "em": 1.0,
    "h": 60.0,
    "eh": 60.0,
    "d": None,  # filled from the project's minutes per day
    "ed": 1440.0,
    "w": None,
    "ew": 10080.0,
    "mo": None,
    "emo": 43200.0,
    "y": None,
    "ey": 525600.0,
}


def read_mpp(
    path: Path,
    *,
    code: str,
    report: ImportReport | None = None,
    deadline_hard: bool = True,
) -> ImportedProject:
    report = report if report is not None else ImportReport()
    project = _read_project(path)
    props = project.getProjectProperties()
    minutes_per_day = float(props.getMinutesPerDay() or 480)
    name = str(props.getProjectTitle() or props.getName() or path.stem)
    prefix = f"{code}."
    wbs, leaves = _structure(project, code, prefix)
    summary_ids = {node.id for node in wbs}
    durations: dict[str, int] = {}
    tasks: list[Task] = []
    for node, parent in leaves:
        task, duration = _task(node, parent, code, prefix, minutes_per_day, report, deadline_hard)
        durations[task.id] = duration
        tasks.append(task)
    dependencies = _links(leaves, prefix, summary_ids, durations, minutes_per_day, report)
    resources, demands = _resources(project, prefix, report)
    tasks = [
        t.model_copy(update={"demands": demands.get(t.id, [])}) if t.duration_wd > 0 else t for t in tasks
    ]
    return ImportedProject(
        code=code,
        name=name,
        tasks=tasks,
        wbs=wbs,
        dependencies=dependencies,
        resources=resources,
        status_date=_py_date(props.getStatusDate()),
        start=_py_date(props.getStartDate()),
        finish=_py_date(props.getFinishDate()),
    )


def _read_project(path: Path) -> Any:
    try:
        import jpype
        import mpxj  # noqa: F401
    except ImportError as exc:
        raise ValueError(
            "чтение MPP и MPX требует Java 17 и пакет mpxj: pip install 'SynAPS-ProgramPlan[mpp]'"
        ) from exc
    if not jpype.isJVMStarted():
        jpype.startJVM()
    from org.mpxj.reader import UniversalProjectReader  # type: ignore[import-not-found]

    try:
        project = UniversalProjectReader().read(str(path))
    except Exception as exc:
        raise ValueError(f"{path.name}: MPXJ не прочитал файл ({exc})") from exc
    if project is None:
        raise ValueError(f"{path.name}: MPXJ не распознал формат")
    return project


def _structure(project: Any, code: str, prefix: str) -> tuple[list[WBSNode], list[tuple[Any, str | None]]]:
    wbs: list[WBSNode] = []
    leaves: list[tuple[Any, str | None]] = []
    for node in project.getTasks():
        if node is None or int(node.getUniqueID() or 0) == 0:
            continue
        parent = node.getParentTask()
        parent_uid = int(parent.getUniqueID() or 0) if parent is not None else 0
        parent_id = f"{prefix}w{parent_uid}" if parent_uid else None
        if bool(node.getSummary()):
            level = int(node.getOutlineLevel() or 1)
            wbs.append(
                WBSNode(
                    id=f"{prefix}w{int(node.getUniqueID())}",
                    project_id=code,
                    parent_id=parent_id,
                    code=str(node.getOutlineNumber() or node.getUniqueID()),
                    name=str(node.getName() or node.getUniqueID()),
                    kind=WBSKind.STAGE if level <= 1 else WBSKind.PACKAGE,
                )
            )
        else:
            leaves.append((node, parent_id))
    return wbs, leaves


def _task(
    node: Any,
    parent: str | None,
    code: str,
    prefix: str,
    minutes_per_day: float,
    report: ImportReport,
    deadline_hard: bool,
) -> tuple[Task, int]:
    uid = str(int(node.getUniqueID()))
    task_id = f"{prefix}{uid}"
    days, elapsed = _working_days(node.getDuration(), minutes_per_day)
    milestone = bool(node.getMilestone()) or days == 0
    duration = 0 if milestone else days
    if not milestone and duration == 0:
        duration = 1
        report.note(f"{task_id}: длительность < 1 раб. дня округлена до 1")
    if elapsed:
        report.note(f"{task_id}: календарная длительность приближена рабочими днями ({duration})")
    pct = float(node.getPercentageComplete() or 0)
    actual_start, actual_finish = _py_date(node.getActualStart()), _py_date(node.getActualFinish())
    status = TaskStatus.PLANNED
    if pct >= 100 and actual_start and actual_finish:
        status = TaskStatus.DONE
    elif actual_start is not None and pct < 100:
        status = TaskStatus.IN_PROGRESS
    remaining = None
    if status is TaskStatus.IN_PROGRESS:
        left, _ = _working_days(node.getRemainingDuration(), minutes_per_day)
        remaining = max(1, left) if left else None
        actual_finish = None
    earliest, latest, pinned = _constraint(node, task_id, report)
    deadline = _py_date(node.getDeadline())
    task = Task(
        id=task_id,
        project_id=code,
        wbs_id=parent,
        name=str(node.getName() or task_id),
        duration_wd=duration,
        kind=TaskKind.MILESTONE if milestone else TaskKind.WORK,
        earliest_start=earliest,
        latest_finish=latest,
        deadline=deadline if deadline_hard else None,
        due_date=None if deadline_hard else deadline,
        pinned=pinned,
        status=status,
        remaining_wd=remaining,
        actual_start=actual_start if status is not TaskStatus.PLANNED else None,
        actual_finish=actual_finish if status is TaskStatus.DONE else None,
        planned_start=_py_date(node.getStart()),
        planned_finish=_py_date(node.getFinish()),
        domain_attributes={"mpp_uid": uid, "wbs": str(node.getWBS() or "")},
    )
    return task, duration


def _constraint(node: Any, task_id: str, report: ImportReport) -> tuple[date | None, date | None, bool]:
    kind = str(node.getConstraintType() or "AS_SOON_AS_POSSIBLE")
    when = _py_date(node.getConstraintDate())
    if kind in ("MUST_START_ON", "START_ON"):
        return when, None, True
    if kind in ("MUST_FINISH_ON", "FINISH_ON"):
        return _py_date(node.getStart()), when, False
    if kind == "START_NO_EARLIER_THAN":
        return when, None, False
    if kind == "FINISH_NO_LATER_THAN":
        return None, when, False
    if kind in ("AS_LATE_AS_POSSIBLE", "START_NO_LATER_THAN", "FINISH_NO_EARLIER_THAN"):
        report.note(f"{task_id}: ограничение {kind} не поддерживается и проигнорировано")
    return None, None, False


def _links(
    leaves: list[tuple[Any, str | None]],
    prefix: str,
    summary_ids: set[str],
    durations: dict[str, int],
    minutes_per_day: float,
    report: ImportReport,
) -> list[Dependency]:
    out: list[Dependency] = []
    for node, _parent in leaves:
        dst = f"{prefix}{int(node.getUniqueID())}"
        for relation in node.getPredecessors() or []:
            pred = relation.getPredecessorTask()
            if pred is None:
                continue
            src = f"{prefix}{int(pred.getUniqueID())}"
            if bool(pred.getSummary()) or f"{prefix}w{int(pred.getUniqueID())}" in summary_ids:
                report.note(f"{dst}: связь от суммарной задачи пропущена (свяжите листовые работы)")
                continue
            if src not in durations:
                report.note(f"{dst}: предшественник {pred.getUniqueID()} не найден — пропущено")
                continue
            kind = DependencyType(str(relation.getType() or "FS"))
            lag, elapsed = _working_days(relation.getLag(), minutes_per_day)
            if elapsed:
                report.note(f"{src}->{dst}: календарный лаг приближён рабочими днями ({lag})")
            out.append(Dependency(src_task_id=src, dst_task_id=dst, type=kind, lag_wd=lag))
    return out


def _resources(
    project: Any, prefix: str, report: ImportReport
) -> tuple[list[Resource], dict[str, list[Demand]]]:
    resources: list[Resource] = []
    by_uid: dict[int, Resource] = {}
    for item in project.getResources():
        if item is None or int(item.getUniqueID() or 0) == 0 or not item.getName():
            continue
        kind_name = str(item.getType() or "WORK")
        if kind_name in ("MATERIAL", "COST"):
            report.note(f"ресурс {item.getName()}: материальный/затратный ресурс пропущен")
            continue
        units = _fte(item.getMaxUnits())
        kind = ResourceKind.EQUIPMENT if kind_name == "NON_LABOR" else ResourceKind.PERSON
        if kind is ResourceKind.PERSON and units > FTE_UNITS:
            kind = ResourceKind.GROUP
        resource = Resource(
            id=f"{prefix}r{int(item.getUniqueID())}",
            kind=kind,
            code=str(item.getName()),
            name=str(item.getName()),
            capacity_units=units,
        )
        resources.append(resource)
        by_uid[int(item.getUniqueID())] = resource
    demands: dict[str, list[Demand]] = {}
    for item in project.getResourceAssignments():
        resource_node, task_node = item.getResource(), item.getTask()
        if resource_node is None or task_node is None:
            continue
        found = by_uid.get(int(resource_node.getUniqueID() or 0))
        if found is None or bool(task_node.getSummary()):
            continue
        units = min(_fte(item.getUnits()), found.capacity_units)
        task_id = f"{prefix}{int(task_node.getUniqueID())}"
        demands.setdefault(task_id, []).append(Demand(resource_id=found.id, units=units))
    return resources, demands


def _fte(raw: Any) -> int:
    """MPXJ reports units as a percentage (100 = full time); some files use a fraction."""
    value = float(raw or 100)
    fraction = value / 100 if value > 10 else value
    return max(1, round(fraction * FTE_UNITS))


def _working_days(duration: Any, minutes_per_day: float) -> tuple[int, bool]:
    if duration is None:
        return 0, False
    unit = str(duration.getUnits())
    amount = float(duration.getDuration())
    per_unit = {
        "d": minutes_per_day,
        "w": minutes_per_day * 5,
        "mo": minutes_per_day * 20,
        "y": minutes_per_day * 250,
    }.get(unit, _MINUTES.get(unit))
    if per_unit is None:
        return 0, unit.startswith("e")
    days = amount * per_unit / minutes_per_day
    elapsed = unit.startswith("e")
    if elapsed:
        days *= 5 / 7
    return int(round(days)), elapsed


def _py_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    return date.fromisoformat(text[:10])
