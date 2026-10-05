"""Exact skill -> person binding for fixed task intervals.

The kernel schedules skill demands against a pooled capacity. Pool feasibility
does not imply that concrete people can be named (a vacation belongs to one
person, partial FTE fragments), so binding is solved exactly as an assignment
problem with soft overload: every skill demand gets exactly one qualified
person, per-person daily capacity holds, total overload is minimised. Zero
overload means the binding is exact; otherwise the planner re-solves the
schedule with the named people.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from synaps_programplan.compiler import Compiled
from synaps_programplan.model import OKRProgram


@dataclass
class Binding:
    """``bound``: clash-free part; ``full``: every choice, including clashing ones."""

    bound: dict[str, dict[str, str]]
    overload_units: int
    unbound: list[tuple[str, str]]
    status: str
    full: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        return self.overload_units == 0 and not self.unbound


def bind_exact(
    program: OKRProgram,
    compiled: Compiled,
    positions: dict[str, tuple[int, int]],
    *,
    time_limit_s: float = 10.0,
    seed: int = 42,
) -> Binding:
    horizon = len(compiled.axis)
    tasks = {task.id: task for task in program.tasks}
    free = {rid: list(days) for rid, days in compiled.availability.items()}
    capacity = {r.id: r.capacity_units for r in program.resources}
    for task_id, (start, end) in positions.items():
        for demand in tasks[task_id].demands:
            if demand.resource_id is not None:
                for day in range(max(0, start), min(horizon, end)):
                    free[demand.resource_id][day] -= demand.units

    items: list[tuple[str, str, int, int, int]] = []
    unbound: list[tuple[str, str]] = []
    for task_id, (start, end) in sorted(positions.items()):
        if end <= start:
            continue
        for demand in tasks[task_id].demands:
            if demand.skill_id is None:
                continue
            members = [
                m for m in compiled.skill_members.get(demand.skill_id, []) if capacity[m] >= demand.units
            ]
            if not members:
                unbound.append((task_id, demand.skill_id))
                continue
            items.append((task_id, demand.skill_id, demand.units, max(0, start), min(horizon, end)))
    if not items:
        return Binding(bound={}, overload_units=0, unbound=unbound, status="EMPTY")

    model = cp_model.CpModel()
    choice: dict[tuple[int, str], cp_model.IntVar] = {}
    by_person: dict[str, list[int]] = defaultdict(list)
    for index, (_, skill_id, _, _, _) in enumerate(items):
        members = [m for m in compiled.skill_members[skill_id] if capacity[m] >= items[index][2]]
        literals = []
        for member in members:
            var = model.new_bool_var(f"x{index}_{member}")
            choice[(index, member)] = var
            literals.append(var)
            by_person[member].append(index)
        model.add_exactly_one(literals)

    overload_terms: list[cp_model.IntVar] = []
    for person, indexes in by_person.items():
        points: set[int] = set()
        for index in indexes:
            start, end = items[index][3], items[index][4]
            points.add(start)
            points.update(day for day in range(start + 1, end) if free[person][day] != free[person][day - 1])
        for day in sorted(points):
            active = [i for i in indexes if items[i][3] <= day < items[i][4]]
            if not active:
                continue
            limit = max(0, free[person][day])
            demand_sum = sum(items[i][2] * choice[(i, person)] for i in active)
            if sum(items[i][2] for i in active) <= limit:
                continue
            slack = model.new_int_var(0, sum(items[i][2] for i in active), f"o_{person}_{day}")
            model.add(demand_sum <= limit + slack)
            overload_terms.append(slack)
    model.minimize(sum(overload_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.random_seed = seed
    solver.parameters.num_workers = 1
    code = solver.solve(model)
    if code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Binding(
            bound={},
            overload_units=-1,
            unbound=unbound + [(i[0], i[1]) for i in items],
            status=solver.status_name(code),
        )
    chosen = {index: person for (index, person), var in choice.items() if solver.value(var)}
    overload = int(sum(solver.value(term) for term in overload_terms))
    clashing = _clashing_items(items, chosen, free) if overload else set()
    bound: dict[str, dict[str, str]] = defaultdict(dict)
    full: dict[str, dict[str, str]] = defaultdict(dict)
    for index, person in chosen.items():
        full[items[index][0]][items[index][1]] = person
        if index in clashing:
            unbound.append((items[index][0], items[index][1]))
        else:
            bound[items[index][0]][items[index][1]] = person
    return Binding(
        bound=dict(bound),
        overload_units=overload,
        unbound=unbound,
        status=solver.status_name(code),
        full=dict(full),
    )


def _clashing_items(
    items: list[tuple[str, str, int, int, int]], chosen: dict[int, str], free: dict[str, list[int]]
) -> set[int]:
    """Items active on an overloaded person-day; dropping them leaves a clean partial binding."""
    load: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for index, person in chosen.items():
        _, _, units, start, end = items[index]
        for day in range(start, end):
            load[person][day] += units
    out: set[int] = set()
    for index, person in chosen.items():
        _, _, _, start, end = items[index]
        if any(load[person][day] > max(0, free[person][day]) for day in range(start, end)):
            out.add(index)
    return out
