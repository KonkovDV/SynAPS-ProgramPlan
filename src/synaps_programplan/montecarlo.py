"""Schedule risk on one accepted plan (plan T3.5).

Durations of planned work are drawn from a triangular law. Each draw is
scheduled by a serial generation scheme: the accepted plan fixes the tie-break
order, precedence and the named resources of that plan are respected, and the
plan is not re-optimised. Milestones stay at duration 0.

P50/P80/P90 are sample quantiles of that scheme. They are not a proved
probability and they ignore max-lag edges. A task's criticality index is the
share of draws in which resource-free CPM gives it no float.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from synaps_programplan.compiler import compile_program
from synaps_programplan.cpm import cpm
from synaps_programplan.model import Dependency, OKRProgram, Task, TaskStatus, _anchor_offset
from synaps_programplan.result import PlanResult, TaskPlan

_NOTE = (
    "Квантили — выборка последовательного расписания при треугольных длительностях "
    "на порядке принятого плана. Это не доказанная вероятность и не новый оптимизированный план."
)


@dataclass(frozen=True)
class MilestoneRisk:
    task_id: str
    name: str
    p50: date | None
    p80: date | None
    p90: date | None
    samples: int

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "p50": self.p50.isoformat() if self.p50 else None,
            "p80": self.p80.isoformat() if self.p80 else None,
            "p90": self.p90.isoformat() if self.p90 else None,
            "samples": self.samples,
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

    def as_dict(self) -> dict[str, object]:
        return {
            "runs": self.runs,
            "seed": self.seed,
            "scheduled_runs": self.scheduled_runs,
            "program_finish": {
                key: value.isoformat() if value else None for key, value in self.program_finish.items()
            },
            "milestone_risk": [item.as_dict() for item in self.milestone_risk],
            "criticality": self.criticality,
            "unbound_tasks": self.unbound_tasks,
            "note": self.note,
        }


def simulate(
    program: OKRProgram,
    plan: PlanResult,
    *,
    runs: int = 200,
    seed: int = 42,
    low_factor: float = 0.8,
    high_factor: float = 1.5,
) -> RiskResult:
    if not plan.outcome.ok:
        raise ValueError("risk uses only an accepted plan (outcome.ok)")
    if runs < 1:
        raise ValueError("runs must be positive")
    compiled = compile_program(program)
    tasks = {task.id: task for task in program.tasks}
    rows = {row.task_id: row for row in plan.tasks}
    order = _sequence(program, rows)
    incoming: dict[str, list[Dependency]] = defaultdict(list)
    for edge in program.dependencies:
        if edge.hard and edge.max_lag_wd is None:
            incoming[edge.dst_task_id].append(edge)
    needs, unbound = _needs(tasks, rows)
    horizon = len(compiled.axis)
    template = {rid: list(days) for rid, days in compiled.availability.items()}
    rng = random.Random(seed)
    base = {task.id: task.duration_wd for task in program.tasks}
    finishes: dict[str, list[int]] = defaultdict(list)
    program_ends: list[int] = []
    critical_hits: dict[str, int] = defaultdict(int)
    scheduled = 0
    for _ in range(runs):
        durations = _draw(rng, tasks, low_factor, high_factor)
        placed = _schedule(order, durations, incoming, needs, template, horizon)
        if placed is None:
            continue
        scheduled += 1
        end_of_run = 0
        for task_id, start in placed.items():
            end = start if durations[task_id] == 0 else start + durations[task_id]
            finishes[task_id].append(end)
            end_of_run = max(end_of_run, end)
        program_ends.append(end_of_run)
        result = cpm(
            durations,
            _constraints(program, durations),
            {task_id: (1 if durations[task_id] == 0 else 0) for task_id in durations},
        )
        for task_id, slack in result.total_float.items():
            if slack <= 0:
                critical_hits[task_id] += 1
    axis = compiled.axis

    def quantile(values: list[int], fraction: float) -> date | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return axis.event_date(min(ordered[index], len(axis)))

    milestones = [
        MilestoneRisk(
            task.id,
            task.name,
            quantile(finishes[task.id], 0.5),
            quantile(finishes[task.id], 0.8),
            quantile(finishes[task.id], 0.9),
            len(finishes[task.id]),
        )
        for task in program.tasks
        if task.duration_wd == 0
    ]
    if scheduled == 0:
        criticality: dict[str, float] = {}
        note = (
            _NOTE
            + " Ни одна выборка не поместилась в горизонт: квантили пустые, индекс критичности не считается."
        )
    else:
        criticality = {task_id: round(critical_hits[task_id] / scheduled, 3) for task_id in base}
        note = _NOTE
        if scheduled < runs:
            note += f" В горизонт поместилось {scheduled} из {runs} выборок."
    return RiskResult(
        runs=runs,
        seed=seed,
        scheduled_runs=scheduled,
        milestone_risk=milestones,
        criticality=criticality,
        program_finish={
            "p50": quantile(program_ends, 0.5),
            "p80": quantile(program_ends, 0.8),
            "p90": quantile(program_ends, 0.9),
        },
        note=note,
        unbound_tasks=unbound,
    )


def _draw(
    rng: random.Random, tasks: dict[str, Task], low_factor: float, high_factor: float
) -> dict[str, int]:
    out: dict[str, int] = {}
    for task_id, task in tasks.items():
        mode = task.duration_wd
        if mode == 0 or task.status is not TaskStatus.PLANNED:
            if task.status is TaskStatus.IN_PROGRESS and task.remaining_wd is not None:
                out[task_id] = task.remaining_wd
            else:
                out[task_id] = mode
            continue
        low = max(1, round(mode * low_factor))
        high = max(low, round(mode * high_factor))
        out[task_id] = int(round(rng.triangular(low, high, mode))) if high > low else mode
    return out


def _sequence(program: OKRProgram, rows: dict[str, TaskPlan]) -> list[str]:
    """Predecessors first; independent tasks keep the accepted plan's start order."""
    indeg = {task.id: 0 for task in program.tasks}
    succ: dict[str, list[str]] = defaultdict(list)
    for edge in program.dependencies:
        if not edge.hard or edge.max_lag_wd is not None:
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


def _schedule(
    order: list[str],
    durations: dict[str, int],
    incoming: dict[str, list[Dependency]],
    needs: dict[str, list[tuple[str, int]]],
    template: dict[str, list[int]],
    horizon: int,
) -> dict[str, int] | None:
    free = {rid: list(days) for rid, days in template.items()}
    placed: dict[str, int] = {}
    for task_id in order:
        duration = durations[task_id]
        earliest = 1 if duration == 0 else 0
        for edge in incoming[task_id]:
            if edge.src_task_id not in placed:
                return None
            earliest = max(
                earliest,
                placed[edge.src_task_id]
                + _anchor_offset(edge.type, durations[edge.src_task_id], duration)
                + edge.lag_wd,
            )
        if duration == 0:
            if earliest > horizon:
                return None
            placed[task_id] = earliest
            continue
        start = _fit(free, needs[task_id], earliest, duration, horizon)
        if start is None:
            return None
        for resource_id, units in needs[task_id]:
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
        if not edge.hard or edge.max_lag_wd is not None:
            continue
        base = _anchor_offset(edge.type, durations[edge.src_task_id], durations[edge.dst_task_id])
        out.append((edge.src_task_id, edge.dst_task_id, base + edge.lag_wd))
    return out
