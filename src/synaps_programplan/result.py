"""Plan result contract: dated tasks, violations, KPIs, explanations, evidence."""

from __future__ import annotations

import datetime as _dt
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from synaps_programplan.model import TaskStatus


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskPlan(_Strict):
    task_id: str
    project_id: str
    name: str
    start: date
    finish: date
    duration_wd: int
    status: TaskStatus
    is_milestone: bool
    bound: dict[str, str] = Field(default_factory=dict)
    start_index: int
    end_index: int
    reference_start: date | None = None
    reference_finish: date | None = None
    shift_wd: int | None = None
    critical: bool = False
    total_float_wd: int | None = None


class Severity(StrEnum):
    HARD = "hard"
    KPI = "kpi"
    WARNING = "warning"


class Violation(_Strict):
    code: str
    severity: Severity
    message: str
    task_ids: list[str] = Field(default_factory=list)
    resource_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Claim(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    HEURISTIC_FEASIBLE = "HEURISTIC_FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class Outcome(_Strict):
    ok: bool
    claim: Claim
    solver_status: str
    solver_config: str
    kernel_verified: bool
    domain_hard_violations: int
    gap: float | None = None
    detail: str = ""


class MilestoneKPI(_Strict):
    task_id: str
    name: str
    date: _dt.date
    target: _dt.date | None
    hard: bool
    lateness_wd: int


class ResourceKPI(_Strict):
    resource_id: str
    peak_pct: float
    mean_pct: float
    overload_days: int


class KPI(_Strict):
    program_finish: date | None
    project_finish: dict[str, date]
    milestones: list[MilestoneKPI]
    late_count: int
    tardiness_wd: int
    resources: list[ResourceKPI]
    moved_count: int
    shift_sum_wd: int
    shift_max_wd: int


class CauseKind(StrEnum):
    """Stable type of one explanation. The last two are reserved for later models."""

    PRECEDENCE = "PRECEDENCE"
    MAX_LAG = "MAX_LAG"
    RESOURCE_CAPACITY = "RESOURCE_CAPACITY"
    SKILL_CAPACITY = "SKILL_CAPACITY"
    CALENDAR = "CALENDAR"
    MAINTENANCE = "MAINTENANCE"
    FREEZE_WINDOW = "FREEZE_WINDOW"
    BASELINE = "BASELINE"
    TEST_ARTICLE = "TEST_ARTICLE"
    INFEASIBILITY_CONFLICT = "INFEASIBILITY_CONFLICT"
    COUNTERFACTUAL = "COUNTERFACTUAL"
    STATUS_DATE = "STATUS_DATE"
    EARLIEST_START = "EARLIEST_START"
    IN_PROGRESS = "IN_PROGRESS"
    OPTIMIZER_CHOICE = "OPTIMIZER_CHOICE"
    # Reserved: changeover (PR-04) and execution modes (PR-03). Not emitted yet.
    SETUP_TRANSITION = "SETUP_TRANSITION"
    MODE_SELECTION = "MODE_SELECTION"


class ExplanationFact(_Strict):
    """Fields a sentence is built from. A date that is not in the plan fails the check."""

    kind: CauseKind
    resource_id: str | None = None
    project_id: str | None = None
    blocker_task_id: str | None = None
    dates: list[str] = Field(default_factory=list)
    plan_hash: str
    input_hash: str


class Explanation(_Strict):
    task_id: str
    shift_wd: int
    cause_code: str
    cause_refs: list[str] = Field(default_factory=list)
    chain: list[str] = Field(default_factory=list)
    text: str
    counterfactual: dict[str, Any] | None = None
    fact: ExplanationFact | None = None


class PlanResult(_Strict):
    schema_version: str = "SynAPS-ProgramPlan.result.v1"
    scenario_id: str
    label: str
    tasks: list[TaskPlan]
    violations: list[Violation]
    outcome: Outcome
    kpi: KPI | None = None
    explanations: list[Explanation] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def task(self, task_id: str) -> TaskPlan:
        for row in self.tasks:
            if row.task_id == task_id:
                return row
        raise KeyError(task_id)
