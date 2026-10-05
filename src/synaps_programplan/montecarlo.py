"""Schedule risk on one accepted plan (plan T3.5, requirement F3).

Durations of not-yet-started work are drawn from a triangular law; named risk
drivers (``OKRProgram.risk_drivers``, AACE 57R-09 style) occur with their
probability and multiply the duration of their tasks by a triangular factor.
Each draw is scheduled by a serial generation scheme: the accepted plan fixes
the tie-break order, precedence, start windows and the named resources of that
plan are respected, and the plan is not re-optimised. Milestones stay at
duration 0, finished work does not move.

P50/P80/P90 are sample quantiles of that scheme and ``on_time_share`` is a
sample frequency; neither is a proved probability. Every hard link keeps its
type and minimum lag; the upper bound of a max-lag link is not modelled. A
task's criticality index is the share of draws in which resource-free CPM
gives it no float. Driver ranking re-runs the same draws
(common random numbers) with one driver switched off and reports how many
working days the P80 program finish gains.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from synaps_programplan.compiler import Compiled, compile_program
from synaps_programplan.cpm import cpm
from synaps_programplan.model import Dependency, OKRProgram, RiskDriver, Task, TaskStatus, _anchor_offset
from synaps_programplan.result import PlanResult, TaskPlan

_NOTE = (
    "Квантили и доли — выборка последовательного расписания при треугольных длительностях "
    "и драйверах риска на порядке принятого плана. Это не доказанная вероятность и не новый "
    "оптимизированный план."
)


@dataclass(frozen=True)
class MilestoneRisk:
    task_id: str
    name: str
    p50: date | None
    p80: date | None
    p90: date | None
    samples: int
    target: date | None = None
    hard: bool = False
    on_time_share: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "p50": self.p50.isoformat() if self.p50 else None,
            "p80": self.p80.isoformat() if self.p80 else None,
            "p90": self.p90.isoformat() if self.p90 else None,
            "samples": self.samples,
            "target": self.target.isoformat() if self.target else None,
            "hard": self.hard,
            "on_time_share": self.on_time_share,
        }


@dataclass(frozen=True)
class DriverImpact:
    driver_id: str
    name: str
    probability: float
    occurred_share: float
    p80_without: date | None
    p80_gain_wd: int
    tasks: int

    def as_dict(self) -> dict[str, object]:
        return {
            "driver_id": self.driver_id,
            "name": self.name,
            "probability": self.probability,
            "occurred_share": self.occurred_share,
            "p80_without": self.p80_without.isoformat() if self.p80_without else None,
            "p80_gain_wd": self.p80_gain_wd,
            "tasks": self.tasks,
        }


@dataclass
class RiskResult:
    runs: int
    seed: int
    scheduled_runs: int
    milestone_risk: list[MilestoneRisk]
    criticality: dict[str, float]
    program_finish: dict[str, date | None]
    note: str = _NOTE
    unbound_tasks: list[str] = field(default_factory=list)
    drivers: list[DriverImpact] = field(default_factory=list)
    deadlines_met_share: float | None = None
    scenario_id: str = ""
    plan_hash: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "plan_hash": self.plan_hash,
            "runs": self.runs,
            "seed": self.seed,
            "scheduled_runs": self.scheduled_runs,
            "program_finish": {
                key: value.isoformat() if value else None for key, value in self.program_finish.items()
            },
            "deadlines_met_share": self.deadlines_met_share,
            "milestone_risk": [item.as_dict() for item in self.milestone_risk],
            "drivers": [item.as_dict() for item in self.drivers],
            "criticality": self.criticality,
            "unbound_tasks": self.unbound_tasks,
            "note": self.note,
        }


@dataclass
class _Runs:
    scheduled: int = 0
    finishes: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    program_ends: list[int] = field(default_factory=list)
    critical_hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    deadline_hits: int = 0
    on_time: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    occurred: dict[str, int] = field(default_factory=lambda: defaultdict(int))


@dataclass
class _Model:
    program: OKRProgram
    compiled: Compiled
    tasks: dict[str, Task]
    order: list[str]
    incoming: dict[str, list[Dependency]]
    needs: dict[str, list[tuple[str, int]]]
    lower: dict[str, int]
    targets: dict[str, int]
    hard_targets: dict[str, int]
    template: dict[str, list[int]]
    horizon: int


def simulate(
    program: OKRProgram,
    plan: PlanResult,
    *,
    runs: int = 200,
    seed: int = 42,
    low_factor: float = 0.8,
    high_factor: float = 1.5,
    rank_drivers: bool = True,
) -> RiskResult:
    if not plan.outcome.ok:
        raise ValueError("risk uses only an accepted plan (outcome.ok)")
    if runs < 1:
        raise ValueError("runs must be positive")
    model, unbound = _model(program, plan)
    drivers = program.risk_drivers
    sample = _run(model, drivers, frozenset(), runs, seed, low_factor, high_factor, with_cpm=True)
    axis = model.compiled.axis

    def quantile(values: list[int], fraction: float) -> date | None:
        if not values:
            return None
        return axis.event_date(min(_rank(values, fraction), len(axis)))

    milestones: list[MilestoneRisk] = []
    for task in program.tasks:
        if task.duration_wd != 0:
            continue
        values = sample.finishes[task.id]
        target = task.hard_finish or task.due_date
        milestones.append(
            MilestoneRisk(
                task.id,
                task.name,
                quantile(values, 0.5),
                quantile(values, 0.8),
                quantile(values, 0.9),
                len(values),
                target=target,
                hard=task.hard_finish is not None,
                on_time_share=round(sample.on_time[task.id] / runs, 3) if task.id in model.targets else None,
            )
        )
    impacts: list[DriverImpact] = []
    if rank_drivers and drivers and sample.program_ends:
        p80_all = _rank(sample.program_ends, 0.8)
        for driver in drivers:
            without = _run(
                model, drivers, frozenset({driver.id}), runs, seed, low_factor, high_factor, with_cpm=False
            )
            p80 = _rank(without.program_ends, 0.8) if without.program_ends else None
            impacts.append(
                DriverImpact(
                    driver_id=driver.id,
                    name=driver.name,
                    probability=driver.probability,
                    occurred_share=round(sample.occurred[driver.id] / runs, 3),
                    p80_without=axis.event_date(min(p80, len(axis))) if p80 is not None else None,
                    p80_gain_wd=p80_all - p80 if p80 is not None else 0,
                    tasks=len(driver.task_ids),
                )
            )
        impacts.sort(key=lambda item: (-item.p80_gain_wd, item.driver_id))
    note = _NOTE
    if sample.scheduled == 0:
        criticality: dict[str, float] = {}
        note += (
            " Ни одна выборка не поместилась в горизонт: квантили пустые, индекс критичности не считается."
        )
    else:
        criticality = {
            task_id: round(sample.critical_hits[task_id] / sample.scheduled, 3) for task_id in model.tasks
        }
        if sample.scheduled < runs:
            note += (
                f" В горизонт поместилось {sample.scheduled} из {runs} выборок; "
                "не поместившиеся считаются несоблюдением сроков."
            )
    return RiskResult(
        runs=runs,
        seed=seed,
        scheduled_runs=sample.scheduled,
        milestone_risk=milestones,
        criticality=criticality,
        program_finish={
            "p50": quantile(sample.program_ends, 0.5),
            "p80": quantile(sample.program_ends, 0.8),
            "p90": quantile(sample.program_ends, 0.9),
        },
        note=note,
        unbound_tasks=unbound,
        drivers=impacts,
        deadlines_met_share=round(sample.deadline_hits / runs, 3) if model.hard_targets else None,
        scenario_id=plan.scenario_id,
        plan_hash=plan.evidence.get("plan_hash"),
    )


def _rank(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def _model(program: OKRProgram, plan: PlanResult) -> tuple[_Model, list[str]]:
    compiled = compile_program(program)
    axis = compiled.axis
    tasks = {task.id: task for task in program.tasks}
    rows = {row.task_id: row for row in plan.tasks}
    incoming: dict[str, list[Dependency]] = defaultdict(list)
    for edge in program.dependencies:
        if edge.hard:
            incoming[edge.dst_task_id].append(edge)
    needs, unbound = _needs(tasks, rows)
    targets: dict[str, int] = {}
    hard_targets: dict[str, int] = {}
    for task in program.tasks:
        if task.status is TaskStatus.DONE:
            continue
        target = task.hard_finish or task.due_date
        if target is not None:
            targets[task.id] = axis.boundary_after(target)
        if task.hard_finish is not None:
            hard_targets[task.id] = axis.boundary_after(task.hard_finish)
    lower = {task_id: window.lo for task_id, window in compiled.windows.items()}
    model = _Model(
        program=program,
        compiled=compiled,
        tasks=tasks,
        order=_sequence(program, rows),
        incoming=incoming,
        needs=needs,
        lower=lower,
        targets=targets,
        hard_targets=hard_targets,
        template={rid: list(days) for rid, days in compiled.availability.items()},
        horizon=len(axis),
    )
    return model, unbound


def _run(
    model: _Model,
    drivers: list[RiskDriver],
    disabled: frozenset[str],
    runs: int,
    seed: int,
    low_factor: float,
    high_factor: float,
    *,
    with_cpm: bool,
) -> _Runs:
    rng = random.Random(seed)
    out = _Runs()
    for _ in range(runs):
        raw = _draw(rng, model.tasks, low_factor, high_factor)
        multiplier: dict[str, float] = defaultdict(lambda: 1.0)
        for driver in drivers:
            hit = rng.random() < driver.probability
            factor = rng.triangular(driver.low, driver.high, driver.mode)
            if not hit:
                continue
            out.occurred[driver.id] += 1
            if driver.id in disabled:
                continue
            for task_id in driver.task_ids:
                multiplier[task_id] *= factor
        durations = {
            task_id: _scaled(model.tasks[task_id], value, multiplier.get(task_id, 1.0))
            for task_id, value in raw.items()
        }
        placed = _schedule(model, durations)
        if placed is None:
            continue
        out.scheduled += 1
        end_of_run = 0
        ends: dict[str, int] = {}
        for task_id, start in placed.items():
            end = start if durations[task_id] == 0 else start + durations[task_id]
            ends[task_id] = end
            if model.tasks[task_id].status is TaskStatus.DONE:
                continue
            out.finishes[task_id].append(end)
            end_of_run = max(end_of_run, end)
        out.program_ends.append(end_of_run)
        for task_id, bound in model.targets.items():
            if ends[task_id] <= bound:
                out.on_time[task_id] += 1
        if all(ends[task_id] <= bound for task_id, bound in model.hard_targets.items()):
            out.deadline_hits += 1
        if with_cpm:
            result = cpm(
                durations,
                _constraints(model.program, durations),
                {task_id: (1 if durations[task_id] == 0 else 0) for task_id in durations},
            )
            for task_id, slack in result.total_float.items():
                if slack <= 0:
                    out.critical_hits[task_id] += 1
    return out


def _draw(
    rng: random.Random, tasks: dict[str, Task], low_factor: float, high_factor: float
) -> dict[str, float]:
    out: dict[str, float] = {}
    for task_id, task in tasks.items():
        mode = task.duration_wd
        if task.status is TaskStatus.DONE or mode == 0:
            out[task_id] = 0.0
            continue
        if task.status is TaskStatus.IN_PROGRESS:
            out[task_id] = float(task.remaining_wd if task.remaining_wd is not None else mode)
            continue
        low = max(1, round(mode * low_factor))
        high = max(low, round(mode * high_factor))
        out[task_id] = rng.triangular(low, high, mode) if high > low else float(mode)
    return out


def _scaled(task: Task, value: float, multiplier: float) -> int:
    if task.status is TaskStatus.DONE or task.duration_wd == 0:
        return 0
    return max(1, int(round(value * multiplier))) if value > 0 else 0


def _sequence(program: OKRProgram, rows: dict[str, TaskPlan]) -> list[str]:
    """Predecessors first; independent tasks keep the accepted plan's start order."""
    indeg = {task.id: 0 for task in program.tasks}
    succ: dict[str, list[str]] = defaultdict(list)
    for edge in program.dependencies:
        if not edge.hard:
            continue
        if edge.src_task_id in indeg and edge.dst_task_id in indeg:
            succ[edge.src_task_id].append(edge.dst_task_id)
            indeg[edge.dst_task_id] += 1

    def _key(task_id: str) -> tuple[int, str]:
        return (rows[task_id].start_index if task_id in rows else 0, task_id)

    ready = sorted([task_id for task_id, degree in indeg.items() if degree == 0], key=_key)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in succ[current]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=_key)
    return order if len(order) == len(indeg) else sorted(indeg, key=_key)


def _needs(
    tasks: dict[str, Task], rows: dict[str, TaskPlan]
) -> tuple[dict[str, list[tuple[str, int]]], list[str]]:
    needs: dict[str, list[tuple[str, int]]] = {}
    unbound: list[str] = []
    for task_id, task in tasks.items():
        if task.status is TaskStatus.DONE:
            needs[task_id] = []
            continue
        row = rows.get(task_id)
        bound = row.bound if row is not None else {}
        pairs: list[tuple[str, int]] = []
        missed = False
        for demand in task.demands:
            if demand.resource_id is not None:
                pairs.append((demand.resource_id, demand.units))
            elif demand.skill_id is not None:
                person = bound.get(demand.skill_id)
                if person is None:
                    missed = True
                else:
                    pairs.append((person, demand.units))
        needs[task_id] = pairs
        if missed:
            unbound.append(task_id)
    return needs, unbound


def _schedule(model: _Model, durations: dict[str, int]) -> dict[str, int] | None:
    free = {rid: list(days) for rid, days in model.template.items()}
    placed: dict[str, int] = {}
    horizon = model.horizon
    for task_id in model.order:
        duration = durations[task_id]
        if model.tasks[task_id].status is TaskStatus.DONE:
            placed[task_id] = 0
            continue
        earliest = model.lower.get(task_id, 1 if duration == 0 else 0)
        for edge in model.incoming[task_id]:
            if edge.src_task_id not in placed:
                return None
            if model.tasks[edge.src_task_id].status is TaskStatus.DONE:
                continue
            earliest = max(
                earliest,
                placed[edge.src_task_id]
                + _anchor_offset(edge.type, durations[edge.src_task_id], duration)
                + edge.lag_wd,
            )
        if duration == 0:
            earliest = max(earliest, 1)
            if earliest > horizon:
                return None
            placed[task_id] = earliest
            continue
        start = _fit(free, model.needs[task_id], earliest, duration, horizon)
        if start is None:
            return None
        for resource_id, units in model.needs[task_id]:
            row = free[resource_id]
            for day in range(start, start + duration):
                row[day] -= units
        placed[task_id] = start
    return placed


def _fit(
    free: dict[str, list[int]], needs: list[tuple[str, int]], earliest: int, duration: int, horizon: int
) -> int | None:
    candidate = max(0, earliest)
    last = horizon - duration
    while candidate <= last:
        blocked = -1
        for resource_id, units in needs:
            row = free[resource_id]
            for day in range(candidate, candidate + duration):
                if row[day] < units:
                    blocked = day
                    break
        if blocked < 0:
            return candidate
        candidate = blocked + 1
    return None


def _constraints(program: OKRProgram, durations: dict[str, int]) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for edge in program.dependencies:
        if not edge.hard:
            continue
        base = _anchor_offset(edge.type, durations[edge.src_task_id], durations[edge.dst_task_id])
        out.append((edge.src_task_id, edge.dst_task_id, base + edge.lag_wd))
    return out
