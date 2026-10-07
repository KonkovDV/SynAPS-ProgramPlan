"""MS Project XML (MSPDI) import and export.

Import maps one MSPDI file to one OKR project:

* summary tasks -> WBS nodes (outline hierarchy), leaf tasks -> tasks;
* PredecessorLink Type 0/1/2/3 -> FF/FS/SF/SS, LinkLag (tenths of minutes,
  working time) -> working days; percent lags use the predecessor duration;
  elapsed lags are approximated and reported;
* ConstraintType SNET -> earliest start, FNLT -> latest finish, MSO -> pinned,
  MFO -> latest finish + earliest start; Deadline -> hard deadline (or a soft
  due date with ``deadline_hard=False``); SNLT / FNET / ALAP are reported as
  unsupported and ignored;
* work resources -> people (MaxUnits 1.0 = 10 units of 0.1 FTE), material
  resources are ignored; assignments -> demands;
* PercentComplete / ActualStart / ActualFinish -> status.

Every approximation is listed in ``ImportReport.notes`` - nothing is silent.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # noqa: S405 - used for writing; parsing goes through defusedxml
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from defusedxml import ElementTree as SafeET

from synaps_programplan.model import (
    FTE_UNITS,
    Demand,
    Dependency,
    DependencyType,
    OKRProgram,
    Resource,
    ResourceKind,
    Task,
    TaskKind,
    TaskStatus,
    WBSKind,
    WBSNode,
)
from synaps_programplan.result import PlanResult, TaskPlan

NS = "http://schemas.microsoft.com/project"
_LINK = {"0": DependencyType.FF, "1": DependencyType.FS, "2": DependencyType.SF, "3": DependencyType.SS}
_ELAPSED_FORMATS = {"4", "6", "8", "10", "12", "36", "38", "40", "42", "44"}
_PERCENT_FORMATS = {"19", "20"}
_DURATION = re.compile(r"^-?PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$")


@dataclass
class ImportedProject:
    code: str
    name: str
    tasks: list[Task]
    wbs: list[WBSNode]
    dependencies: list[Dependency]
    resources: list[Resource]
    status_date: date | None
    start: date | None
    finish: date | None
    source_hash: str = ""


@dataclass(frozen=True)
class ImportLoss:
    """One thing the import did not carry across unchanged."""

    code: str
    object_id: str
    action: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "object_id": self.object_id,
            "action": self.action,
            "message": self.message,
        }


@dataclass
class ImportReport:
    notes: list[str] = field(default_factory=list)
    losses: list[ImportLoss] = field(default_factory=list)

    def note(
        self,
        text: str,
        *,
        code: str = "NOTE",
        object_id: str = "",
        action: str = "info",
    ) -> None:
        """``action`` is ``skipped``, ``approximated``, ``clamped`` or ``info``.

        Only ``info`` is allowed through ``--strict``. The others are losses.
        """
        self.notes.append(text)
        self.losses.append(ImportLoss(code=code, object_id=object_id, action=action, message=text))

    def blocking(self) -> list[ImportLoss]:
        return [item for item in self.losses if item.action != "info"]


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _text(node: ET.Element, tag: str) -> str | None:
    child = node.find(_q(tag))
    return child.text.strip() if child is not None and child.text else None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value).date()


def duration_minutes(value: str | None) -> float:
    if not value:
        return 0.0
    match = _DURATION.match(value)
    if not match:
        raise ValueError(f"unsupported MSPDI duration {value!r}")
    hours, minutes, seconds = (float(part) if part else 0.0 for part in match.groups())
    total = hours * 60 + minutes + seconds / 60
    return -total if value.startswith("-") else total


def read_mspdi(
    path: Path,
    *,
    code: str,
    report: ImportReport | None = None,
    deadline_hard: bool = True,
) -> ImportedProject:
    report = report if report is not None else ImportReport()
    root = SafeET.parse(path).getroot()
    if root is None or root.tag != _q("Project"):
        raise ValueError(f"{path.name}: not an MSPDI file (root {getattr(root, 'tag', None)})")
    minutes_per_day = float(_text(root, "MinutesPerDay") or 480)
    name = _text(root, "Title") or _text(root, "Name") or path.stem
    prefix = f"{code}."
    tasks_node = root.find(_q("Tasks"))
    raw = list(tasks_node) if tasks_node is not None else []
    summary_uids: set[str] = set()
    wbs: list[WBSNode] = []
    outline_parent: dict[int, str] = {}
    leaf: list[tuple[ET.Element, str | None]] = []
    for node in raw:
        uid = _text(node, "UID") or ""
        if uid == "0" or _text(node, "IsNull") == "1":
            continue
        level = int(_text(node, "OutlineLevel") or 1)
        parent = outline_parent.get(level - 1)
        if _text(node, "Summary") == "1":
            summary_uids.add(uid)
            wbs_id = f"{prefix}w{uid}"
            wbs.append(
                WBSNode(
                    id=wbs_id,
                    project_id=code,
                    parent_id=parent,
                    code=_text(node, "OutlineNumber") or uid,
                    name=_text(node, "Name") or uid,
                    kind=WBSKind.STAGE if level == 1 else WBSKind.PACKAGE,
                )
            )
            outline_parent[level] = wbs_id
            for deeper in [k for k in outline_parent if k > level]:
                del outline_parent[deeper]
        else:
            leaf.append((node, parent))
    durations: dict[str, int] = {}
    tasks: list[Task] = []
    links: list[tuple[str, ET.Element]] = []
    for node, parent in leaf:
        task, duration = _task(node, parent, code, prefix, minutes_per_day, report, deadline_hard)
        durations[task.id] = duration
        tasks.append(task)
        links.extend((task.id, link) for link in node.findall(_q("PredecessorLink")))
    dependencies = _links(links, prefix, summary_uids, durations, minutes_per_day, report)
    resources, demands = _resources(root, prefix, report)
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
        status_date=_date(_text(root, "StatusDate")),
        start=_date(_text(root, "StartDate")),
        finish=_date(_text(root, "FinishDate")),
    )


def _task(
    node: ET.Element,
    parent: str | None,
    code: str,
    prefix: str,
    minutes_per_day: float,
    report: ImportReport,
    deadline_hard: bool,
) -> tuple[Task, int]:
    uid = _text(node, "UID") or ""
    task_id = f"{prefix}{uid}"
    name = _text(node, "Name") or task_id
    minutes = duration_minutes(_text(node, "Duration"))
    duration = round(minutes / minutes_per_day)
    milestone = _text(node, "Milestone") == "1" or minutes == 0
    if milestone:
        duration = 0
    elif duration == 0:
        duration = 1
        report.note(
            f"{task_id}: длительность < 1 раб. дня округлена до 1",
            code="DURATION_ROUNDED",
            object_id=task_id,
            action="approximated",
        )
    start, finish = _date(_text(node, "Start")), _date(_text(node, "Finish"))
    actual_start, actual_finish = _date(_text(node, "ActualStart")), _date(_text(node, "ActualFinish"))
    raw_percent = _text(node, "PercentComplete")
    percent = int(float(raw_percent)) if raw_percent not in (None, "") else None
    pct = 0 if percent is None else percent
    status = TaskStatus.PLANNED
    if pct >= 100 and actual_start and actual_finish:
        status = TaskStatus.DONE
    elif actual_start is not None and pct < 100:
        status = TaskStatus.IN_PROGRESS
    remaining = None
    if status is TaskStatus.IN_PROGRESS:
        remaining_minutes = duration_minutes(_text(node, "RemainingDuration"))
        remaining = max(1, round(remaining_minutes / minutes_per_day)) if remaining_minutes else None
        actual_finish = None
    constraint = _text(node, "ConstraintType") or "0"
    constraint_date = _date(_text(node, "ConstraintDate"))
    earliest = latest = None
    pinned = False
    if constraint == "4":
        earliest = constraint_date
    elif constraint == "7":
        latest = constraint_date
    elif constraint == "2":
        pinned = True
    elif constraint == "3":
        latest = constraint_date
        earliest = start
    elif constraint in ("1", "5", "6"):
        label = {"1": "ALAP", "5": "SNLT", "6": "FNET"}[constraint]
        report.note(
            f"{task_id}: ограничение {label} не поддерживается и проигнорировано",
            code="CONSTRAINT_UNSUPPORTED",
            object_id=task_id,
            action="skipped",
        )
    deadline = _date(_text(node, "Deadline"))
    task = Task(
        id=task_id,
        project_id=code,
        wbs_id=parent,
        name=name,
        duration_wd=duration,
        kind=TaskKind.MILESTONE if milestone else TaskKind.WORK,
        earliest_start=earliest,
        latest_finish=latest,
        deadline=deadline if deadline_hard else None,
        due_date=None if deadline_hard else deadline,
        pinned=pinned,
        status=status,
        remaining_wd=remaining,
        percent_complete=_stored_percent(status, percent),
        actual_start=actual_start if status is not TaskStatus.PLANNED else None,
        actual_finish=actual_finish if status is TaskStatus.DONE else None,
        planned_start=start,
        planned_finish=finish,
        baseline_start=_date(_baseline(node, "Start")),
        baseline_finish=_date(_baseline(node, "Finish")),
        domain_attributes={"mspdi_uid": uid, "wbs": _text(node, "WBS")},
    )
    return task, duration


def _stored_percent(status: TaskStatus, percent: int | None) -> int | None:
    if status is TaskStatus.DONE:
        return 100
    if status is TaskStatus.IN_PROGRESS and percent is not None and percent < 100:
        return percent
    if status is TaskStatus.PLANNED and percent == 0:
        return 0
    return None


def _baseline(node: ET.Element, tag: str) -> str | None:
    for baseline in node.findall(_q("Baseline")):
        if (_text(baseline, "Number") or "0") == "0":
            return _text(baseline, tag)
    return None


def _links(
    links: list[tuple[str, ET.Element]],
    prefix: str,
    summary_uids: set[str],
    durations: dict[str, int],
    minutes_per_day: float,
    report: ImportReport,
) -> list[Dependency]:
    out: list[Dependency] = []
    for dst, link in links:
        pred = _text(link, "PredecessorUID") or ""
        src = f"{prefix}{pred}"
        if pred in summary_uids:
            report.note(
                f"{dst}: связь от суммарной задачи {pred} пропущена (свяжите листовые работы)",
                code="SUMMARY_LINK_SKIPPED",
                object_id=dst,
                action="skipped",
            )
            continue
        if src not in durations:
            report.note(
                f"{dst}: предшественник {pred} не найден (внешняя связь?) — пропущено",
                code="PREDECESSOR_MISSING",
                object_id=dst,
                action="skipped",
            )
            continue
        kind = _LINK.get(_text(link, "Type") or "1", DependencyType.FS)
        lag_raw = float(_text(link, "LinkLag") or 0)
        lag_format = _text(link, "LagFormat") or "7"
        if lag_format in _PERCENT_FORMATS:
            lag = round(durations[src] * lag_raw / 100)
        else:
            lag = round(lag_raw / 10 / minutes_per_day)
            if lag_format in _ELAPSED_FORMATS:
                lag = round(lag * 5 / 7)
                report.note(
                    f"{src}->{dst}: календарный лаг приближён рабочими днями ({lag})",
                    code="ELAPSED_LAG",
                    object_id=f"{src}->{dst}",
                    action="approximated",
                )
        out.append(Dependency(src_task_id=src, dst_task_id=dst, type=kind, lag_wd=lag))
    return out


def _resources(
    root: ET.Element, prefix: str, report: ImportReport
) -> tuple[list[Resource], dict[str, list[Demand]]]:
    resources: list[Resource] = []
    by_uid: dict[str, Resource] = {}
    node = root.find(_q("Resources"))
    for item in list(node) if node is not None else []:
        uid = _text(item, "UID") or ""
        name = _text(item, "Name")
        if uid == "0" or not name:
            continue
        if (_text(item, "Type") or "1") != "1":
            report.note(
                f"ресурс {name}: материальный/затратный ресурс пропущен",
                code="MATERIAL_RESOURCE",
                object_id=name,
                action="skipped",
            )
            continue
        units = max(1, round(float(_text(item, "MaxUnits") or 1) * FTE_UNITS))
        kind = ResourceKind.GROUP if units > FTE_UNITS else ResourceKind.PERSON
        resource = Resource(id=f"{prefix}r{uid}", kind=kind, code=name, name=name, capacity_units=units)
        resources.append(resource)
        by_uid[uid] = resource
    demands: dict[str, list[Demand]] = {}
    node = root.find(_q("Assignments"))
    for item in list(node) if node is not None else []:
        res_uid = _text(item, "ResourceUID") or ""
        if res_uid not in by_uid:
            continue
        task_id = f"{prefix}{_text(item, 'TaskUID')}"
        units = max(1, round(float(_text(item, "Units") or 1) * FTE_UNITS))
        resource = by_uid[res_uid]
        if units > resource.capacity_units:
            report.note(
                f"{task_id}: назначение {resource.code} {units / FTE_UNITS:g} > MaxUnits, ограничено",
                code="ASSIGNMENT_CLAMPED",
                object_id=task_id,
                action="clamped",
            )
            units = resource.capacity_units
        demands.setdefault(task_id, []).append(Demand(resource_id=resource.id, units=units))
    return resources, demands


# ---------- export ----------


def write_plan_mspdi(
    program: OKRProgram, plan: PlanResult, path: Path, *, minutes_per_day: int = 480
) -> None:
    """Write an accepted plan as MSPDI (OKR -> WBS -> task) for MS Project.

    Tasks are written as manually scheduled with the planned dates, so MS Project
    shows exactly the verified plan instead of re-levelling it.
    """
    if not plan.outcome.ok:
        raise ValueError("only an accepted plan (outcome.ok) can be exported")
    ET.register_namespace("", NS)
    root = ET.Element(_q("Project"))
    for tag, value in (
        ("Name", program.program.name),
        ("Title", program.program.name),
        ("MinutesPerDay", minutes_per_day),
        ("StartDate", f"{program.program.horizon_start}T09:00:00"),
        ("FinishDate", f"{program.program.horizon_end}T18:00:00"),
        ("StatusDate", f"{program.program.planning_start}T09:00:00"),
    ):
        ET.SubElement(root, _q(tag)).text = str(value)
    tasks_node = ET.SubElement(root, _q("Tasks"))
    rows = {row.task_id: row for row in plan.tasks}
    uid: dict[str, int] = {}
    wbs_by_project: dict[str, list[WBSNode]] = {}
    for node in program.wbs:
        wbs_by_project.setdefault(node.project_id, []).append(node)
    counter = 0

    def emit(
        key: str,
        name: str,
        level: int,
        summary: bool,
        start: date,
        finish: date,
        duration: int,
        milestone: bool,
    ) -> ET.Element:
        nonlocal counter
        counter += 1
        uid[key] = counter
        task = ET.SubElement(tasks_node, _q("Task"))
        for tag, value in (
            ("UID", counter),
            ("ID", counter),
            ("Name", name),
            ("OutlineLevel", level),
            ("Summary", int(summary)),
            ("Milestone", int(milestone)),
            ("Manual", int(not summary)),
            ("Start", f"{start}T09:00:00"),
            ("Finish", f"{finish}T18:00:00"),
            ("Duration", f"PT{duration * minutes_per_day // 60}H0M0S"),
            ("DurationFormat", 7),
        ):
            ET.SubElement(task, _q(tag)).text = str(value)
        return task

    elements: dict[str, ET.Element] = {}
    for project in program.projects:
        own = [row for row in plan.tasks if row.project_id == project.id]
        if not own:
            continue
        emit(
            f"p:{project.id}",
            f"{project.code} {project.name}",
            1,
            True,
            min(r.start for r in own),
            max(r.finish for r in own),
            0,
            False,
        )
        groups: list[tuple[WBSNode | None, list[TaskPlan]]] = []
        for wbs_node in wbs_by_project.get(project.id, []):
            members = [r for r in own if program.task(r.task_id).wbs_id == wbs_node.id]
            if members:
                groups.append((wbs_node, members))
        loose = [r for r in own if program.task(r.task_id).wbs_id not in {n.id for n, _ in groups if n}]
        if loose:
            groups.append((None, loose))
        for group_node, members in groups:
            level = 2
            if group_node is not None:
                emit(
                    f"w:{group_node.id}",
                    group_node.name,
                    2,
                    True,
                    min(r.start for r in members),
                    max(r.finish for r in members),
                    0,
                    False,
                )
                level = 3
            for row in members:
                element = emit(
                    row.task_id,
                    row.name,
                    level,
                    False,
                    row.start,
                    row.finish,
                    row.duration_wd,
                    row.is_milestone,
                )
                elements[row.task_id] = element
                if row.reference_start is not None and row.reference_finish is not None:
                    baseline = ET.SubElement(element, _q("Baseline"))
                    ET.SubElement(baseline, _q("Number")).text = "0"
                    ET.SubElement(baseline, _q("Start")).text = f"{row.reference_start}T09:00:00"
                    ET.SubElement(baseline, _q("Finish")).text = f"{row.reference_finish}T18:00:00"
                source = program.task(row.task_id)
                if source.hard_finish is not None:
                    ET.SubElement(element, _q("Deadline")).text = f"{source.hard_finish}T18:00:00"
                ET.SubElement(element, _q("PercentComplete")).text = str(source.progress_percent())
                if source.actual_start is not None:
                    ET.SubElement(element, _q("ActualStart")).text = f"{source.actual_start}T09:00:00"
                if source.actual_finish is not None:
                    ET.SubElement(element, _q("ActualFinish")).text = f"{source.actual_finish}T18:00:00"
                if source.status is TaskStatus.IN_PROGRESS and source.remaining_wd is not None:
                    hours = source.remaining_wd * minutes_per_day // 60
                    ET.SubElement(element, _q("RemainingDuration")).text = f"PT{hours}H0M0S"
                reason = next((item.text for item in plan.explanations if item.task_id == row.task_id), "")
                stamp = str(plan.evidence.get("plan_hash") or "")
                ET.SubElement(element, _q("Notes")).text = f"plan_hash {stamp}. {reason}".strip()
    reverse = {v: k for k, v in _LINK.items()}
    for edge in program.dependencies:
        if edge.dst_task_id not in elements or edge.src_task_id not in rows:
            continue
        link = ET.SubElement(elements[edge.dst_task_id], _q("PredecessorLink"))
        ET.SubElement(link, _q("PredecessorUID")).text = str(uid[edge.src_task_id])
        ET.SubElement(link, _q("Type")).text = reverse[edge.type]
        ET.SubElement(link, _q("LinkLag")).text = str(edge.lag_wd * minutes_per_day * 10)
        ET.SubElement(link, _q("LagFormat")).text = "7"
    resources_node = ET.SubElement(root, _q("Resources"))
    resource_uid: dict[str, int] = {}
    for index, resource in enumerate(program.resources, start=1):
        resource_uid[resource.id] = index
        resource_node = ET.SubElement(resources_node, _q("Resource"))
        ET.SubElement(resource_node, _q("UID")).text = str(index)
        ET.SubElement(resource_node, _q("Name")).text = resource.name
        ET.SubElement(resource_node, _q("Type")).text = "1"
        ET.SubElement(resource_node, _q("MaxUnits")).text = str(resource.capacity_units / FTE_UNITS)
    assignments = ET.SubElement(root, _q("Assignments"))
    for row in plan.tasks:
        if row.task_id not in uid:
            continue
        for demand in program.task(row.task_id).demands:
            if demand.resource_id not in resource_uid:
                continue
            item = ET.SubElement(assignments, _q("Assignment"))
            ET.SubElement(item, _q("TaskUID")).text = str(uid[row.task_id])
            ET.SubElement(item, _q("ResourceUID")).text = str(resource_uid[demand.resource_id])
            ET.SubElement(item, _q("Units")).text = str(demand.units / FTE_UNITS)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
