"""Load and dates of an accepted plan, rolled up by the executing enterprise."""

from __future__ import annotations

from synaps_programplan.model import OKRProgram, ResourceKind
from synaps_programplan.result import PlanResult


def enterprise_rollups(program: OKRProgram, result: PlanResult) -> list[dict[str, object]]:
    """One row per enterprise: projects, dates and stand demand in working days.

    Projects with no enterprise are reported together under an empty name.
    """
    by_project = {row.task_id: row for row in result.tasks}
    stands = {resource.id for resource in program.resources if resource.kind is ResourceKind.STAND}
    grouped: dict[str, list[str]] = {}
    for project in program.projects:
        grouped.setdefault(project.enterprise or "", []).append(project.id)
    rows: list[dict[str, object]] = []
    for enterprise, project_ids in grouped.items():
        owned = {task.id for task in program.tasks if task.project_id in project_ids}
        planned = [by_project[task_id] for task_id in owned if task_id in by_project]
        starts = [row.start for row in planned]
        finishes = [row.finish for row in planned]
        demand = 0
        for task in program.tasks:
            if task.id not in owned:
                continue
            for item in task.demands:
                if item.resource_id in stands:
                    demand += task.duration_wd * item.units
        rows.append(
            {
                "enterprise": enterprise,
                "projects": project_ids,
                "tasks": len(owned),
                "start": min(starts) if starts else None,
                "finish": max(finishes) if finishes else None,
                "stand_demand_wd": demand,
            }
        )
    rows.sort(key=lambda row: str(row["enterprise"]))
    return rows
