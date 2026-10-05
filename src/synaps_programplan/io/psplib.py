"""PSPLIB loaders for the public benchmarks.

``.sm``: single-mode RCPSP of Kolisch & Sprecher (1997) - jobs with durations,
renewable resources with constant availability, FS precedence.

``.sch``: RCPSP/max in the ProGen/max format (Schwindt 1995; test sets
j10/j20/j30, UBO, c/d) - start-to-start time lags that may be negative, i.e.
minimum and maximum time lags. Some instances are infeasible by design.

In both the dummy source is dropped and the sink stays as the milestone
``end``, so the makespan is its date index and the ``finish`` objective (zero
due date on sinks) minimises exactly it. The axis is working days; holidays do
not change a working-day makespan because availability is constant and there
are no calendar exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from synaps_programplan.calendar import WorkCalendar, WorkdayAxis
from synaps_programplan.model import (
    Calendar,
    CalendarBase,
    Demand,
    Dependency,
    DependencyType,
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


@dataclass(frozen=True)
class MaxInstance:
    """RCPSP/max: ``start(j) - start(i) >= lag`` for every ``(i, j, lag)``."""

    name: str
    jobs: int
    durations: dict[int, int]
    lags: list[tuple[int, int, int]]
    requests: dict[int, list[int]]
    capacities: list[int]


class InstanceInfeasible(ValueError):
    """No schedule exists; the message is the proof, not a parse error."""


class TemporallyInfeasible(InstanceInfeasible):
    """The time lags alone contain a positive cycle."""


def parse_sch(text: str, name: str = "instance") -> MaxInstance:
    rows = [line.split() for line in text.splitlines() if line.strip()]
    if not rows or len(rows[0]) < 2:
        raise ValueError(f"{name}: not a ProGen/max .sch file")
    real, kinds = int(rows[0][0]), int(rows[0][1])
    jobs = real + 2
    if len(rows) < 2 * jobs + 2:
        raise ValueError(f"{name}: expected {jobs} activities, file is truncated")
    lags: list[tuple[int, int, int]] = []
    for row in rows[1 : 1 + jobs]:
        job, count = int(row[0]), int(row[2])
        successors = [int(x) for x in row[3 : 3 + count]]
        values = [int(x.strip("[]")) for x in row[3 + count : 3 + 2 * count]]
        if len(values) != count:
            raise ValueError(f"{name}: activity {job} lists {count} successors but {len(values)} lags")
        lags.extend((job, succ, lag) for succ, lag in zip(successors, values, strict=True))
    durations: dict[int, int] = {}
    requests: dict[int, list[int]] = {}
    for row in rows[1 + jobs : 1 + 2 * jobs]:
        durations[int(row[0])] = int(row[2])
        requests[int(row[0])] = [int(x) for x in row[3 : 3 + kinds]]
    capacities = [int(x) for x in rows[1 + 2 * jobs][:kinds]]
    return MaxInstance(name, jobs, durations, lags, requests, capacities)


def earliest_starts(instance: MaxInstance) -> dict[int, int]:
    """Longest paths from the source over the time lags (Bellman-Ford)."""
    dist = dict.fromkeys(instance.durations, 0)
    for _ in range(instance.jobs):
        changed = False
        for src, dst, lag in instance.lags:
            if dist[src] + lag > dist[dst]:
                dist[dst] = dist[src] + lag
                changed = True
        if not changed:
            return dist
    raise TemporallyInfeasible(f"{instance.name}: time lags contain a positive cycle")


def max_to_program(instance: MaxInstance, *, start: date = date(2027, 1, 11)) -> OKRProgram:
    """RCPSP/max -> program.

    The program model keeps dependencies acyclic, so every pair of activities
    is oriented along the earliest-start order: a lag in that direction is the
    minimum SS lag, a lag against it becomes the maximum SS lag of the forward
    link. A pair bound only from behind gets the horizon as its (inactive)
    minimum lag. Every activity gets an FS link into ``end``; a lag into the
    sink longer than the activity becomes the lag of that link.
    """
    source, sink = 0, instance.jobs - 1
    es = earliest_starts(instance)
    rank = {job: index for index, job in enumerate(sorted(es, key=lambda j: (es[j], j)))}
    horizon = sum(instance.durations.values()) + sum(max(lag, 0) for _, _, lag in instance.lags)
    calendar = Calendar(id="ru", base=CalendarBase.RU_PRODUCTION)
    end_day = start + timedelta(days=int(horizon * 1.6) + 60)
    axis = WorkdayAxis.build(WorkCalendar.from_model(calendar), start, end_day)
    release: dict[int, int] = {}
    pairs: dict[tuple[int, int], list[int | None]] = {}
    to_sink: dict[int, int] = {}
    for src, dst, lag in instance.lags:
        if src == source:
            release[dst] = max(release.get(dst, 0), lag)
            continue
        if dst == sink:
            to_sink[src] = max(to_sink.get(src, lag), lag)
            continue
        if src == sink or dst == source:
            raise ValueError(f"{instance.name}: lags out of the sink or into the source are not supported")
        if rank[src] < rank[dst]:
            bounds = pairs.setdefault((src, dst), [None, None])
            bounds[0] = lag if bounds[0] is None else max(bounds[0], lag)
        else:
            bounds = pairs.setdefault((dst, src), [None, None])
            bounds[1] = -lag if bounds[1] is None else min(bounds[1], -lag)
    tasks = []
    for job in sorted(instance.durations):
        if job in (source, sink):
            continue
        duration = instance.durations[job]
        lower = release.get(job, 0)
        tasks.append(
            Task(
                id=f"a{job}",
                project_id="p",
                name=f"activity {job}",
                duration_wd=duration,
                kind=TaskKind.WORK,
                demands=[
                    Demand(resource_id=f"R{k + 1}", units=units)
                    for k, units in enumerate(instance.requests[job])
                    if units > 0
                ],
                earliest_start=axis.start_date(lower) if lower > 0 else None,
            )
        )
    for job, requested in instance.requests.items():
        if job in (source, sink):
            continue
        for index, units in enumerate(requested):
            cap = instance.capacities[index]
            if units > cap:
                raise InstanceInfeasible(
                    f"{instance.name}: activity {job} needs {units} of R{index + 1}, capacity is {cap}"
                )
    tasks.append(Task(id="end", project_id="p", name="end", duration_wd=0, kind=TaskKind.MILESTONE))
    deps = [
        Dependency(
            src_task_id=f"a{a}",
            dst_task_id=f"a{b}",
            type=DependencyType.SS,
            lag_wd=-horizon if low is None else low,
            max_lag_wd=high,
        )
        for (a, b), (low, high) in sorted(pairs.items())
    ]
    # Every activity closes before the sink: the only terminal task is ``end``,
    # so the ``finish`` objective is exactly the makespan.
    deps += [
        Dependency(
            src_task_id=f"a{job}",
            dst_task_id="end",
            lag_wd=max(0, to_sink.get(job, 0) - instance.durations[job]),
        )
        for job in sorted(instance.durations)
        if job not in (source, sink)
    ]
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
    return OKRProgram(
        program=Program(
            id=instance.name,
            name=f"RCPSP/max {instance.name}",
            calendar_id="ru",
            horizon_start=start,
            horizon_end=end_day,
            status_date=start,
        ),
        calendars=[calendar],
        projects=[Project(id="p", code="P", name=instance.name)],
        tasks=tasks,
        dependencies=deps,
        resources=resources,
        provenance=Provenance(kind=ProvenanceKind.OPEN_DATA, source=f"RCPSP/max {instance.name}"),
    )


def load_sch(path: Path) -> OKRProgram:
    return max_to_program(parse_sch(path.read_text(encoding="utf-8", errors="replace"), name=path.stem))


@dataclass(frozen=True)
class Reference:
    """Published result: ``lower == upper`` is the optimum, ``infeasible`` is proven."""

    lower: int | None = None
    upper: int | None = None
    infeasible: bool = False


def read_max_reference(path: Path) -> dict[str, Reference]:
    """kobe-scheduling ``optimum.csv``: ``problem,optimum`` with a number, ``lb..ub`` or ``unsat``."""
    out: dict[str, Reference] = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or parts[0].lower() == "problem":
            continue
        key = Path(parts[0]).stem.lower()
        value = parts[1].lower()
        if value == "unsat":
            out[key] = Reference(infeasible=True)
        elif ".." in value:
            low, high = value.split("..", 1)
            out[key] = Reference(lower=int(low), upper=int(high))
        elif value.isdigit():
            out[key] = Reference(lower=int(value), upper=int(value))
    return out


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
