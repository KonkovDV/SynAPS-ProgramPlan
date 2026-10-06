"""Primavera P6 XER import (requirement F1: plans from different systems).

One XER file may carry several projects; every PROJECT row becomes one OKR
project and TASKPRED rows whose predecessor lives in another project become
CROSS_PROJECT links - Primavera keeps those natively, MS Project files do not.

Mapping (every approximation goes to ``ImportReport.notes``):

* PROJWBS -> WBS nodes (the project root node is dropped);
* TASK: TT_Task / TT_Rsrc -> work, TT_Mile / TT_FinMile -> milestone,
  TT_LOE and TT_WBS are skipped (level-of-effort / summary have no own
  duration); durations are hours / ``CALENDAR.day_hr_cnt`` (8 h by default);
* status TK_Complete -> DONE, TK_Active -> IN_PROGRESS with remaining
  duration; early (or target) dates -> source-plan dates;
* constraints: CS_MSOA -> earliest start, CS_MEOB -> latest finish,
  CS_MSO / CS_MANDSTART -> pinned, CS_MEO / CS_MANDFIN -> latest finish;
  CS_MSOB, CS_MEOA, CS_ALAP are reported and ignored;
* TASKPRED PR_FS/SS/FF/SF with ``lag_hr_cnt`` -> working-day lags (the
  predecessor's calendar);
* RSRC RT_Labor -> people / groups, RT_Equip -> equipment (test stands),
  RT_Mat is skipped; RSRCRATE.max_qty_per_hr -> capacity (1.0 = one full
  unit), TASKRSRC.target_qty_per_hr -> demand units.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from synaps_programplan.io.mspdi import ImportedProject, ImportReport
from synaps_programplan.model import (
    FTE_UNITS,
    Demand,
    Dependency,
    DependencySource,
    DependencyType,
    Resource,
    ResourceKind,
    Task,
    TaskKind,
    TaskStatus,
    WBSKind,
    WBSNode,
)

Table = list[dict[str, str]]
_LINK = {
    "PR_FS": DependencyType.FS,
    "PR_SS": DependencyType.SS,
    "PR_FF": DependencyType.FF,
    "PR_SF": DependencyType.SF,
}
_IGNORED_CONSTRAINTS = {"CS_MSOB": "start on or before", "CS_MEOA": "finish on or after", "CS_ALAP": "ALAP"}


def parse_xer(text: str) -> dict[str, Table]:
    """Tables of an XER file: ``%T`` name, ``%F`` fields, ``%R`` rows."""
    tables: dict[str, Table] = {}
    name: str | None = None
    fields: list[str] = []
    for line in text.splitlines():
        if not line:
            continue
        tag, _, rest = line.partition("\t")
        if tag == "%T":
            name = rest.strip()
            tables.setdefault(name, [])
            fields = []
        elif tag == "%F":
            fields = rest.split("\t")
        elif tag == "%R" and name is not None:
            values = rest.split("\t")
            tables[name].append(
                {key: (values[i] if i < len(values) else "").strip() for i, key in enumerate(fields)}
            )
    if "TASK" not in tables:
        raise ValueError("not an XER file: no TASK table")
    return tables


def read_xer_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    return date.fromisoformat(value.strip()[:10])


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _code(text: str) -> str:
    return re.sub(r"\s+", "_", text.strip()) or "proj"


def read_xer(
    path: Path,
    *,
    codes: dict[str, str] | None = None,
    report: ImportReport | None = None,
) -> tuple[list[ImportedProject], list[Dependency]]:
    """Projects of one XER file plus the cross-project links between them."""
    report = report if report is not None else ImportReport()
    tables = parse_xer(read_xer_text(path))
    projects = tables.get("PROJECT", [])
    if not projects:
        raise ValueError(f"{path.name}: no PROJECT rows")
    code_of = {
        row["proj_id"]: (codes or {}).get(
            row.get("proj_short_name", ""), _code(row.get("proj_short_name") or row["proj_id"])
        )
        for row in projects
    }
    hours = {
        row.get("clndr_id", ""): _float(row.get("day_hr_cnt"), 8.0) or 8.0
        for row in tables.get("CALENDAR", [])
    }
    wbs_rows = tables.get("PROJWBS", [])
    roots = {row["wbs_id"] for row in wbs_rows if row.get("proj_node_flag") == "Y"}
    wbs_by_project: dict[str, list[WBSNode]] = defaultdict(list)
    for row in wbs_rows:
        if row["wbs_id"] in roots or row.get("proj_id") not in code_of:
            continue
        code = code_of[row["proj_id"]]
        parent = row.get("parent_wbs_id") or None
        wbs_by_project[row["proj_id"]].append(
            WBSNode(
                id=f"{code}.w{row['wbs_id']}",
                project_id=code,
                parent_id=None if parent is None or parent in roots else f"{code}.w{parent}",
                code=row.get("wbs_short_name") or row["wbs_id"],
                name=row.get("wbs_name") or row.get("wbs_short_name") or row["wbs_id"],
                kind=WBSKind.STAGE if parent in roots else WBSKind.PACKAGE,
            )
        )
    known_wbs = {node.id for nodes in wbs_by_project.values() for node in nodes}

    resources, demands = _resources(tables, code_of, report)
    task_ids: dict[str, str] = {}
    task_hpd: dict[str, float] = {}
    durations: dict[str, int] = {}
    tasks_by_project: dict[str, list[Task]] = defaultdict(list)
    seen: set[str] = set()
    for row in tables["TASK"]:
        proj = row.get("proj_id", "")
        if proj not in code_of:
            continue
        code = code_of[proj]
        kind = row.get("task_type", "TT_Task")
        label = row.get("task_code") or row["task_id"]
        if kind in ("TT_LOE", "TT_WBS"):
            report.note(
                f"{code}.{label}: {kind} (уровень усилий / суммарная) пропущена",
                code="LOE_OR_SUMMARY",
                object_id=f"{code}.{label}",
                action="skipped",
            )
            continue
        task_id = f"{code}.{label}"
        if task_id in seen:
            task_id = f"{code}.{label}#{row['task_id']}"
        seen.add(task_id)
        hpd = hours.get(row.get("clndr_id", ""), 8.0)
        task = _task(row, task_id, code, hpd, known_wbs, demands.get(row["task_id"], []), report)
        task_ids[row["task_id"]] = task_id
        task_hpd[row["task_id"]] = hpd
        durations[task_id] = task.duration_wd
        tasks_by_project[proj].append(task)

    internal: dict[str, list[Dependency]] = defaultdict(list)
    cross: list[Dependency] = []
    for row in tables.get("TASKPRED", []):
        src, dst = task_ids.get(row.get("pred_task_id", "")), task_ids.get(row.get("task_id", ""))
        if src is None or dst is None:
            report.note(
                f"связь {row.get('pred_task_id')}->{row.get('task_id')}: работа не импортирована — пропущено",
                code="PREDECESSOR_MISSING",
                object_id=f"{row.get('pred_task_id')}->{row.get('task_id')}",
                action="skipped",
            )
            continue
        link_type = _LINK.get(row.get("pred_type", "PR_FS"))
        if link_type is None:
            report.note(
                f"{src}->{dst}: тип связи {row.get('pred_type')} неизвестен — принят FS",
                code="UNKNOWN_LINK_TYPE",
                object_id=f"{src}->{dst}",
                action="approximated",
            )
            link_type = DependencyType.FS
        lag = round(_float(row.get("lag_hr_cnt")) / task_hpd.get(row.get("pred_task_id", ""), 8.0))
        is_cross = row.get("pred_proj_id", row.get("proj_id")) != row.get("proj_id")
        edge = Dependency(
            src_task_id=src,
            dst_task_id=dst,
            type=link_type,
            lag_wd=lag,
            source=DependencySource.CROSS_PROJECT if is_cross else DependencySource.IMPORTED,
        )
        if is_cross:
            cross.append(edge)
        else:
            internal[row.get("proj_id", "")].append(edge)

    out: list[ImportedProject] = []
    for row in projects:
        proj = row["proj_id"]
        code = code_of[proj]
        own_tasks = tasks_by_project.get(proj, [])
        starts = [t.planned_start for t in own_tasks if t.planned_start]
        finishes = [t.planned_finish for t in own_tasks if t.planned_finish]
        used = {d.resource_id for t in own_tasks for d in t.demands}
        out.append(
            ImportedProject(
                code=code,
                name=row.get("proj_short_name") or code,
                tasks=own_tasks,
                wbs=wbs_by_project.get(proj, []),
                dependencies=internal.get(proj, []),
                resources=[r for r in resources if r.id in used],
                status_date=_date(row.get("last_recalc_date")) or _date(row.get("next_data_date")),
                start=_date(row.get("plan_start_date")) or (min(starts) if starts else None),
                finish=_date(row.get("scd_end_date")) or (max(finishes) if finishes else None),
            )
        )
    report.note(
        "XER: базовый план Primavera хранится отдельным проектом и не импортирован; "
        "эталон — даты текущего плана",
        code="BASELINE_NOT_IN_FILE",
        action="info",
    )
    return out, cross


def _task(
    row: dict[str, str],
    task_id: str,
    code: str,
    hpd: float,
    known_wbs: set[str],
    demands: list[Demand],
    report: ImportReport,
) -> Task:
    kind = row.get("task_type", "TT_Task")
    duration = round(_float(row.get("target_drtn_hr_cnt")) / hpd)
    milestone = kind in ("TT_Mile", "TT_FinMile")
    if not milestone and duration == 0:
        if _float(row.get("target_drtn_hr_cnt")) > 0:
            duration = 1
            report.note(
                f"{task_id}: длительность < 1 раб. дня округлена до 1",
                code="DURATION_ROUNDED",
                object_id=task_id,
                action="approximated",
            )
        else:
            milestone = True
            report.note(
                f"{task_id}: работа нулевой длительности импортирована как веха",
                code="ZERO_DURATION_AS_MILESTONE",
                object_id=task_id,
                action="approximated",
            )
    if milestone:
        duration = 0
    status_code = row.get("status_code", "TK_NotStart")
    act_start, act_end = _date(row.get("act_start_date")), _date(row.get("act_end_date"))
    status = TaskStatus.PLANNED
    remaining = None
    if status_code == "TK_Complete":
        if act_start and act_end:
            status = TaskStatus.DONE
        else:
            report.note(
                f"{task_id}: статус «завершена» без фактических дат — оставлена плановой",
                code="STATUS_WITHOUT_DATES",
                object_id=task_id,
                action="approximated",
            )
    elif status_code == "TK_Active":
        if act_start:
            status = TaskStatus.IN_PROGRESS
            remaining = max(0, round(_float(row.get("remain_drtn_hr_cnt")) / hpd))
        else:
            report.note(
                f"{task_id}: статус «начата» без фактического старта — оставлена плановой",
                code="STATUS_WITHOUT_DATES",
                object_id=task_id,
                action="approximated",
            )
    start = _date(row.get("early_start_date")) or _date(row.get("target_start_date"))
    finish = _date(row.get("early_end_date")) or _date(row.get("target_end_date"))
    earliest = latest = None
    pinned = False
    for ctype, cdate in (
        (row.get("cstr_type"), row.get("cstr_date")),
        (row.get("cstr_type2"), row.get("cstr_date2")),
    ):
        if not ctype:
            continue
        when = _date(cdate)
        if ctype == "CS_MSOA":
            earliest = when
        elif ctype == "CS_MEOB":
            latest = when
        elif ctype in ("CS_MSO", "CS_MANDSTART"):
            pinned = True
        elif ctype in ("CS_MEO", "CS_MANDFIN"):
            latest = when
        elif ctype in _IGNORED_CONSTRAINTS:
            label = _IGNORED_CONSTRAINTS[ctype]
            report.note(
                f"{task_id}: ограничение {ctype} ({label}) не поддерживается — пропущено",
                code="CONSTRAINT_UNSUPPORTED",
                object_id=task_id,
                action="skipped",
            )
        else:
            report.note(
                f"{task_id}: ограничение {ctype} неизвестно — пропущено",
                code="CONSTRAINT_UNKNOWN",
                object_id=task_id,
                action="skipped",
            )
    wbs_id = (
        f"{code}.w{row['wbs_id']}" if row.get("wbs_id") and f"{code}.w{row['wbs_id']}" in known_wbs else None
    )
    return Task(
        id=task_id,
        project_id=code,
        wbs_id=wbs_id,
        name=row.get("task_name") or task_id,
        duration_wd=duration,
        kind=TaskKind.MILESTONE if milestone else TaskKind.WORK,
        demands=[] if milestone else demands,
        earliest_start=earliest,
        latest_finish=latest,
        pinned=pinned,
        status=status,
        remaining_wd=remaining,
        actual_start=act_start if status is not TaskStatus.PLANNED else None,
        actual_finish=act_end if status is TaskStatus.DONE else None,
        planned_start=act_start if status is not TaskStatus.PLANNED and act_start else start,
        planned_finish=act_end if status is TaskStatus.DONE and act_end else finish,
        domain_attributes={"xer_task_id": row["task_id"], "xer_task_code": row.get("task_code", "")},
    )


def _resources(
    tables: dict[str, Table], code_of: dict[str, str], report: ImportReport
) -> tuple[list[Resource], dict[str, list[Demand]]]:
    rates: dict[str, float] = {}
    for row in sorted(tables.get("RSRCRATE", []), key=lambda r: r.get("start_date", "")):
        if row.get("max_qty_per_hr"):
            rates[row["rsrc_id"]] = _float(row["max_qty_per_hr"], 1.0)
    resources: list[Resource] = []
    by_id: dict[str, Resource] = {}
    for row in tables.get("RSRC", []):
        name = row.get("rsrc_name") or row.get("rsrc_short_name") or row["rsrc_id"]
        rtype = row.get("rsrc_type", "RT_Labor")
        if rtype == "RT_Mat":
            report.note(
                f"ресурс {name}: материальный ресурс пропущен",
                code="MATERIAL_RESOURCE",
                object_id=name,
                action="skipped",
            )
            continue
        units = max(1, round(rates.get(row["rsrc_id"], 1.0) * FTE_UNITS))
        if rtype == "RT_Equip":
            kind = ResourceKind.EQUIPMENT
        else:
            kind = ResourceKind.GROUP if units > FTE_UNITS else ResourceKind.PERSON
        resource = Resource(
            id=f"xer.r{row['rsrc_id']}",
            kind=kind,
            code=row.get("rsrc_short_name") or name,
            name=name,
            capacity_units=units,
        )
        resources.append(resource)
        by_id[row["rsrc_id"]] = resource
    demands: dict[str, list[Demand]] = defaultdict(list)
    for row in tables.get("TASKRSRC", []):
        assigned = by_id.get(row.get("rsrc_id", ""))
        if assigned is None or row.get("proj_id") not in code_of:
            continue
        units = max(1, round(_float(row.get("target_qty_per_hr"), 1.0) * FTE_UNITS))
        if units > assigned.capacity_units:
            report.note(
                f"задача {row.get('task_id')}: назначение {assigned.code} больше доступного — ограничено",
                code="ASSIGNMENT_CLAMPED",
                object_id=str(row.get("task_id") or ""),
                action="clamped",
            )
            units = assigned.capacity_units
        demands[row["task_id"]].append(Demand(resource_id=assigned.id, units=units))
    return resources, demands
