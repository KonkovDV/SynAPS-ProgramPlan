"""Data-quality report for a consolidated program (plan T1.6).

These are hygiene warnings. They do not by themselves reject a schedule:
a dangling task can still be placed. Conflict detection lives in
``conflicts.py`` and calls this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from synaps_programplan.calendar import WorkCalendar, WorkdayAxis
from synaps_programplan.model import OKRProgram, TaskStatus


@dataclass(frozen=True)
class QualityLimits:
    """Pilot starting points, not a standard. Override them with the environment."""

    long_task_wd: int = 66
    hard_share: float = 0.05
    hard_min_count: int = 5
    high_lag_wd: int = 20


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def quality_limits() -> QualityLimits:
    return QualityLimits(
        long_task_wd=_env_int("SYNAPS_PROGRAMPLAN_LONG_TASK_WD", 66),
        hard_share=_env_float("SYNAPS_PROGRAMPLAN_HARD_SHARE", 0.05),
        hard_min_count=_env_int("SYNAPS_PROGRAMPLAN_HARD_MIN_COUNT", 5),
        high_lag_wd=_env_int("SYNAPS_PROGRAMPLAN_HIGH_LAG_WD", 20),
    )


@dataclass
class QualityIssue:
    code: str
    message: str
    tasks: list[str] = field(default_factory=list)
    severity: str = "warning"

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "tasks": self.tasks, "severity": self.severity}


def quality_issues(
    program: OKRProgram,
    calendar: WorkCalendar,
    axis: WorkdayAxis,
    limits: QualityLimits | None = None,
) -> list[QualityIssue]:
    """Schedule-quality checks in the spirit of DCMA 14-Point and GAO-16-89G.

    Thresholds are the pilot starting points from ``QualityLimits``. They are
    not a claim that a schedule above them is wrong.
    """
    del axis  # the axis is part of the call from conflict analysis; dates use the calendar
    limits = limits or quality_limits()
    out: list[QualityIssue] = []
    has_pred = {edge.dst_task_id for edge in program.dependencies}
    has_succ = {edge.src_task_id for edge in program.dependencies}
    dangling = [t.id for t in program.tasks if t.id not in has_pred and t.id not in has_succ]
    if dangling:
        out.append(QualityIssue("DANGLING", f"{len(dangling)} работ без связей", dangling[:50]))
    open_end = [
        t.id for t in program.tasks if t.id in has_pred and t.id not in has_succ and t.duration_wd > 0
    ]
    if open_end:
        out.append(
            QualityIssue(
                "OPEN_END", f"{len(open_end)} работ без последователей (не ведут к вехе)", open_end[:50]
            )
        )
    no_res = [t.id for t in program.tasks if t.duration_wd > 0 and not t.demands]
    if no_res:
        out.append(QualityIssue("NO_RESOURCES", f"{len(no_res)} работ без назначенных ресурсов", no_res[:50]))
    long_tasks = [t.id for t in program.tasks if t.duration_wd > limits.long_task_wd]
    if long_tasks:
        out.append(
            QualityIssue(
                "LONG_TASK",
                f"{len(long_tasks)} работ длиннее {limits.long_task_wd} раб. дн.",
                long_tasks[:50],
            )
        )
    leads = [f"{e.src_task_id}->{e.dst_task_id}" for e in program.dependencies if e.lag_wd < 0]
    if leads:
        out.append(QualityIssue("NEGATIVE_LAG", f"{len(leads)} связей с отрицательным лагом", leads[:50]))
    long_lags = [
        f"{e.src_task_id}->{e.dst_task_id}" for e in program.dependencies if e.lag_wd > limits.high_lag_wd
    ]
    if long_lags:
        out.append(
            QualityIssue(
                "HIGH_LAG",
                f"{len(long_lags)} связей с задержкой больше {limits.high_lag_wd} раб. дн.",
                long_lags[:50],
            )
        )
    hard = [t.id for t in program.tasks if t.hard_finish is not None or t.earliest_start is not None]
    hard_limit = max(limits.hard_min_count, int(len(program.tasks) * limits.hard_share))
    if len(hard) > hard_limit:
        out.append(
            QualityIssue(
                "HARD_CONSTRAINTS",
                f"{len(hard)} работ с жёсткими датами (порог {hard_limit})",
                hard[:50],
            )
        )
    start = program.program.planning_start
    stale = [
        t.id
        for t in program.tasks
        if t.status is TaskStatus.PLANNED and t.planned_start is not None and t.planned_start < start
    ]
    if stale:
        out.append(
            QualityIssue(
                "STALE_PLANNED", f"{len(stale)} работ должны были начаться до даты статуса", stale[:50]
            )
        )
    for task in program.tasks:
        for value in (task.planned_start, task.earliest_start):
            if value is not None and task.duration_wd > 0 and not calendar.is_workday(value):
                out.append(
                    QualityIssue(
                        "NONWORKING_DATE",
                        f"дата {value} работы {task.id} — нерабочий день",
                        [task.id],
                        "info",
                    )
                )
                break
    return out


def quality_report(program: OKRProgram) -> dict[str, object]:
    from synaps_programplan.calendar import WorkdayAxis
    from synaps_programplan.compiler import program_calendar

    calendar = program_calendar(program)
    axis = WorkdayAxis.build(calendar, program.program.planning_start, program.program.horizon_end)
    issues = quality_issues(program, calendar, axis)
    return {"count": len(issues), "issues": [item.as_dict() for item in issues]}
