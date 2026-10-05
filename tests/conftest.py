from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pytest

from synaps_okrplan.model import (
    Calendar,
    CalendarBase,
    Demand,
    Dependency,
    DependencyType,
    OKRProgram,
    Program,
    Project,
    Resource,
    ResourceKind,
    Skill,
    Task,
    TaskKind,
)

START = date(2026, 10, 5)  # Monday


@pytest.fixture(autouse=True)
def _quiet_kernel_logs() -> None:
    logging.getLogger("synaps").setLevel(logging.WARNING)


def task(task_id: str, duration: int, project: str = "p1", **extra: Any) -> Task:
    kind = TaskKind.MILESTONE if duration == 0 else TaskKind.WORK
    return Task(id=task_id, project_id=project, name=task_id, duration_wd=duration, kind=kind, **extra)


def stand(resource_id: str = "st", capacity: int = 1) -> Resource:
    return Resource(
        id=resource_id,
        kind=ResourceKind.STAND,
        code=resource_id.upper(),
        name=resource_id,
        capacity_units=capacity,
    )


def person(resource_id: str, skills: list[str] | None = None) -> Resource:
    return Resource(
        id=resource_id,
        kind=ResourceKind.PERSON,
        code=resource_id.upper(),
        name=resource_id,
        capacity_units=10,
        skills=skills or [],
    )


def dep(src: str, dst: str, kind: str = "FS", lag: int = 0, **extra: Any) -> Dependency:
    return Dependency(src_task_id=src, dst_task_id=dst, type=DependencyType(kind), lag_wd=lag, **extra)


def uses(resource_id: str, units: int = 1) -> list[Demand]:
    return [Demand(resource_id=resource_id, units=units)]


def needs(skill_id: str, units: int = 10) -> list[Demand]:
    return [Demand(skill_id=skill_id, units=units)]


def program(
    tasks: list[Task],
    deps: list[Dependency] | None = None,
    resources: list[Resource] | None = None,
    *,
    projects: list[str] | None = None,
    skills: list[str] | None = None,
    calendar: CalendarBase = CalendarBase.FIVE_DAY,
    **extra: Any,
) -> OKRProgram:
    project_ids = projects or sorted({t.project_id for t in tasks})
    return OKRProgram(
        program=Program(
            id="prog",
            name="test",
            calendar_id="cal",
            horizon_start=START,
            horizon_end=date(2027, 12, 31),
            status_date=START,
        ),
        calendars=[Calendar(id="cal", base=calendar)],
        projects=[Project(id=p, code=p.upper(), name=p) for p in project_ids],
        tasks=tasks,
        dependencies=deps or [],
        resources=resources or [],
        skills=[Skill(id=s, code=s, name=s) for s in skills or []],
        **extra,
    )
