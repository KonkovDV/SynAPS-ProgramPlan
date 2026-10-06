"""Merge several project plans into one consolidated OKR program (requirement F1).

* every imported file becomes one OKR project (ids are prefixed by its code);
* resources with the same normalised name are one shared resource: that is
  exactly where cross-project overloads come from;
* cross-project links come from a separate table (CSV), because separate
  MS Project files cannot reference each other's tasks reliably.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from synaps_programplan.io.mspdi import ImportedProject, ImportLoss
from synaps_programplan.model import (
    Calendar,
    CalendarBase,
    Demand,
    Dependency,
    DependencySource,
    DependencyType,
    OKRProgram,
    Program,
    Project,
    Provenance,
    ProvenanceKind,
    Resource,
)


@dataclass
class MergeReport:
    shared_resources: dict[str, list[str]] = field(default_factory=dict)
    capacity_conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    losses: list[ImportLoss] = field(default_factory=list)


def normalise(name: str) -> str:
    return re.sub(r"\s+", " ", name.casefold().replace("ё", "е")).strip()


def _slug(name: str) -> str:
    return re.sub(r"[^\w]+", "_", normalise(name)).strip("_")[:48] or "res"


def merge_projects(
    projects: list[ImportedProject],
    *,
    program_id: str,
    name: str,
    status_date: date | None = None,
    horizon_end: date | None = None,
    links: list[Dependency] | None = None,
    provenance: Provenance | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[OKRProgram, MergeReport]:
    report = MergeReport()
    shared: dict[str, Resource] = {}
    remap: dict[str, str] = {}
    owners: dict[str, list[str]] = {}
    agreed = aliases or {}
    for project in projects:
        for resource in project.resources:
            agreed_name = agreed.get(normalise(resource.name))
            key = normalise(agreed_name or resource.name)
            display = agreed_name or resource.name
            if agreed_name and normalise(resource.name) != key:
                message = f"{project.code}: «{resource.name}» сопоставлено с «{display}»"
                report.notes.append(message)
                report.losses.append(
                    ImportLoss(
                        code="ALIAS_APPLIED",
                        object_id=resource.name,
                        action="info",
                        message=message,
                    )
                )
            existing = shared.get(key)
            if existing is None:
                rid = f"res:{_slug(display)}"
                existing = resource.model_copy(update={"id": rid, "name": display, "code": display})
                shared[key] = existing
            elif existing.capacity_units != resource.capacity_units:
                report.capacity_conflicts.append(
                    f"{resource.name}: MaxUnits {existing.capacity_units / 10:g} vs "
                    f"{resource.capacity_units / 10:g} в {project.code} — взят максимум"
                )
                existing = existing.model_copy(
                    update={"capacity_units": max(existing.capacity_units, resource.capacity_units)}
                )
                shared[key] = existing
            remap[resource.id] = existing.id
            owners.setdefault(existing.id, []).append(project.code)
    report.shared_resources = {
        rid: sorted(set(codes)) for rid, codes in owners.items() if len(set(codes)) > 1
    }
    _note_similar_names(shared, report)

    tasks = []
    wbs = []
    dependencies: list[Dependency] = []
    for project in projects:
        wbs.extend(project.wbs)
        dependencies.extend(project.dependencies)
        for task in project.tasks:
            demands = _merge_demands(
                [
                    Demand(resource_id=remap[d.resource_id], units=d.units) if d.resource_id else d
                    for d in task.demands
                ]
            )
            tasks.append(task.model_copy(update={"demands": demands}))
    known = {task.id for task in tasks}
    for link in links or []:
        if link.src_task_id not in known or link.dst_task_id not in known:
            message = f"межпроектная связь {link.src_task_id}->{link.dst_task_id}: работа не найдена"
            report.notes.append(message)
            report.losses.append(
                ImportLoss(
                    code="CROSS_LINK_MISSING",
                    object_id=f"{link.src_task_id}->{link.dst_task_id}",
                    action="skipped",
                    message=message,
                )
            )
            continue
        dependencies.append(link)

    starts = [p.start for p in projects if p.start] or [date.today()]
    finishes = [p.finish for p in projects if p.finish] or [max(starts)]
    statuses = [p.status_date for p in projects if p.status_date]
    status = status_date or (max(statuses) if statuses else min(starts))
    horizon = horizon_end or date(max(finishes).year + 3, 12, 28)
    program = OKRProgram(
        program=Program(
            id=program_id,
            name=name,
            calendar_id="ru",
            horizon_start=min(starts),
            horizon_end=horizon,
            status_date=status,
        ),
        calendars=[Calendar(id="ru", base=CalendarBase.RU_PRODUCTION)],
        projects=[Project(id=p.code, code=p.code, name=p.name) for p in projects],
        wbs=wbs,
        tasks=tasks,
        dependencies=dependencies,
        resources=sorted({r.id: r for r in shared.values()}.values(), key=lambda r: r.id),
        provenance=provenance or Provenance(kind=ProvenanceKind.EXPERIMENT, source="mspdi merge"),
    )
    return program, report


def _note_similar_names(shared: dict[str, Resource], report: MergeReport) -> None:
    """Suggest a correspondence. Do not merge resources the table did not name."""
    keys = list(shared)
    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            if SequenceMatcher(None, left, right).ratio() < 0.86:
                continue
            message = (
                f"похожие ресурсы «{shared[left].name}» и «{shared[right].name}» "
                "не объединены: добавьте строку в таблицу соответствий"
            )
            report.notes.append(message)
            report.losses.append(
                ImportLoss(
                    code="SIMILAR_RESOURCE",
                    object_id=f"{shared[left].id}|{shared[right].id}",
                    action="info",
                    message=message,
                )
            )


def read_aliases_csv(path: Path) -> dict[str, str]:
    """Columns ``alias`` and ``canonical`` (also ``from`` / ``to``)."""
    out: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample or "alias,canonical\n", delimiters=",;\t")
        for row in csv.DictReader(handle, dialect=dialect):
            source = row.get("alias") or row.get("from") or row.get("name") or ""
            target = row.get("canonical") or row.get("to") or ""
            if source.strip() and target.strip():
                out[normalise(source)] = target.strip()
    return out


def _merge_demands(demands: list[Demand]) -> list[Demand]:
    merged: dict[tuple[str | None, str | None], int] = {}
    for demand in demands:
        key = (demand.resource_id, demand.skill_id)
        merged[key] = merged.get(key, 0) + demand.units
    return [Demand(resource_id=r, skill_id=s, units=u) for (r, s), u in merged.items()]


def read_links_csv(path: Path) -> list[Dependency]:
    """Columns: src_project, src_uid, dst_project, dst_uid, type (FS/SS/FF/SF), lag_wd."""
    out: list[Dependency] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        for row in csv.DictReader(handle, dialect=dialect):
            out.append(
                Dependency(
                    src_task_id=f"{row['src_project'].strip()}.{row['src_uid'].strip()}",
                    dst_task_id=f"{row['dst_project'].strip()}.{row['dst_uid'].strip()}",
                    type=DependencyType((row.get("type") or "FS").strip().upper()),
                    lag_wd=int((row.get("lag_wd") or "0").strip()),
                    source=DependencySource.CROSS_PROJECT,
                )
            )
    return out
