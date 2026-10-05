"""PSPLIB single-mode RCPSP (``.sm``) loader for the public benchmark.

Kolisch & Sprecher (1997) instances: jobs with durations, renewable resources
with constant availability, FS precedence. The dummy source is dropped and the
sink stays as the milestone ``end``, so the makespan is its date index and the
``finish`` objective (zero due date on sinks) minimises exactly it. The
axis is working days; holidays do not change a working-day makespan because
availability is constant and there are no calendar exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from synaps_programplan.model import (
    Calendar,
    CalendarBase,
    Demand,
    Dependency,
    OKRProgram,
    Program,
    Project,
    Provenance,
    ProvenanceKind,
    Resource,
    ResourceKind,
    Task,
    TaskKind,
)


@dataclass(frozen=True)
class PsplibInstance:
    name: str
    jobs: int
    horizon: int
    durations: dict[int, int]
    successors: dict[int, list[int]]
    requests: dict[int, list[int]]
    capacities: list[int]


def parse_sm(text: str, name: str = "instance") -> PsplibInstance:
    lines = text.splitlines()

    def section(title: str) -> int:
        for index, line in enumerate(lines):
            if line.strip().upper().startswith(title):
                return index
        raise ValueError(f"{name}: section {title!r} not found")

    jobs_match = re.search(r"jobs \(incl\. supersource/sink \):\s*(\d+)", text)
    horizon_match = re.search(r"horizon\s*:\s*(\d+)", text)
    if not jobs_match:
        raise ValueError(f"{name}: not a PSPLIB .sm file")
    jobs = int(jobs_match.group(1))
    successors: dict[int, list[int]] = {}
    start = section("PRECEDENCE RELATIONS") + 2
    for line in lines[start : start + jobs]:
        parts = [int(p) for p in line.split()]
        successors[parts[0]] = parts[3 : 3 + parts[2]]
    start = section("REQUESTS/DURATIONS") + 3
    durations: dict[int, int] = {}
    requests: dict[int, list[int]] = {}
    for line in lines[start : start + jobs]:
        parts = [int(p) for p in line.split()]
        durations[parts[0]] = parts[2]
        requests[parts[0]] = parts[3:]
    start = section("RESOURCEAVAILABILITIES") + 2
    capacities = [int(p) for p in lines[start].split()]
    return PsplibInstance(
        name=name,
        jobs=jobs,
        horizon=int(horizon_match.group(1)) if horizon_match else sum(durations.values()),
        durations=durations,
        successors=successors,
        requests=requests,
        capacities=capacities,
    )


def to_program(instance: PsplibInstance, *, start: date = date(2027, 1, 11)) -> OKRProgram:
    source, sink = 1, instance.jobs
    real = [job for job in sorted(instance.durations) if job not in (source, sink)]
    resources = [
        Resource(
            id=f"R{k + 1}",
            kind=ResourceKind.EQUIPMENT,
            code=f"R{k + 1}",
            name=f"R{k + 1}",
            capacity_units=cap,
        )
        for k, cap in enumerate(instance.capacities)
    ]
    tasks = []
    for job in real:
        duration = instance.durations[job]
        demands = [
            Demand(resource_id=f"R{k + 1}", units=units)
            for k, units in enumerate(instance.requests[job])
            if units > 0 and duration > 0
        ]
        tasks.append(
            Task(
                id=f"j{job}",
                project_id="p",
                name=f"job {job}",
                duration_wd=duration,
                kind=TaskKind.MILESTONE if duration == 0 else TaskKind.WORK,
                demands=demands,
            )
        )
    tasks.append(Task(id=f"j{sink}", project_id="p", name="end", duration_wd=0, kind=TaskKind.MILESTONE))
    deps = [
        Dependency(src_task_id=f"j{a}", dst_task_id=f"j{b}")
        for a in real
        for b in instance.successors.get(a, [])
    ]
    horizon_days = int(instance.horizon * 1.6) + 60
    return OKRProgram(
        program=Program(
            id=instance.name,
            name=f"PSPLIB {instance.name}",
            calendar_id="ru",
            horizon_start=start,
            horizon_end=start + timedelta(days=horizon_days),
            status_date=start,
        ),
        calendars=[Calendar(id="ru", base=CalendarBase.RU_PRODUCTION)],
        projects=[Project(id="p", code="P", name=instance.name)],
        tasks=tasks,
        dependencies=deps,
        resources=resources,
        provenance=Provenance(kind=ProvenanceKind.OPEN_DATA, source=f"PSPLIB {instance.name}"),
    )


def load_sm(path: Path) -> OKRProgram:
    return to_program(parse_sm(path.read_text(encoding="utf-8", errors="replace"), name=path.stem))


def read_optimum(path: Path, *, per_parameter: int = 10) -> dict[str, int]:
    """Reference makespans -> {"j301_1": makespan}.

    Two layouts: PSPLIB ``j30opt.sm`` (``Par Inst Makespan ...``) and the
    Solutions Update ``J30_BKS.csv`` (``ID;Type;Value;...``, IDs numbered
    parameter by parameter, ``per_parameter`` instances each).
    """
    out: dict[str, int] = {}
    prefix = re.sub(r"(opt|hrs|lb|_bks)$", "", path.stem.lower())
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".csv":
        for line in text.splitlines():
            parts = line.split(";")
            if len(parts) >= 3 and parts[0].isdigit() and parts[2].isdigit():
                index = int(parts[0]) - 1
                out[f"{prefix}{index // per_parameter + 1}_{index % per_parameter + 1}"] = int(parts[2])
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            out[f"{prefix}{parts[0]}_{parts[1]}"] = int(parts[2])
    return out
