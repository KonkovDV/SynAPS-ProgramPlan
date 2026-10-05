"""Self-contained interactive HTML report (requirement F5).

One file, no CDN, no network: embedded JSON + vanilla JS/SVG. Only plans with
``outcome.ok`` get a Gantt chart; rejected or infeasible scenarios appear in
the comparison table with their verdict and no dates.
"""

from __future__ import annotations

import html
import json
from datetime import date
from importlib import resources
from typing import Any

from synaps_okrplan.calendar import is_provisional
from synaps_okrplan.compiler import compile_program
from synaps_okrplan.conflicts import Analysis
from synaps_okrplan.model import OKRProgram, TaskStatus
from synaps_okrplan.planner import resource_profiles
from synaps_okrplan.result import PlanResult
from synaps_okrplan.scenarios import compare
from synaps_okrplan.versions import CLAIM_LEVEL, ISO16290_TRL, OKRPLAN_VERSION, SYNAPS_COMMIT


def report_data(
    program: OKRProgram,
    plans: list[PlanResult],
    analysis: Analysis | None = None,
    witness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compiled = compile_program(program)
    axis_days = [d.isoformat() for d in compiled.axis.days]
    projects = [
        {
            "id": p.id,
            "code": p.code,
            "name": p.name,
            "deadline": p.deadline.isoformat() if p.deadline else None,
        }
        for p in program.projects
    ]
    tasks = {task.id: task for task in program.tasks}
    resources = [
        {"id": r.id, "code": r.code, "name": r.name, "kind": r.kind.value, "capacity": r.capacity_units}
        for r in program.resources
    ]
    scenarios: list[dict[str, Any]] = []
    for result in plans:
        entry: dict[str, Any] = {
            "id": result.scenario_id,
            "label": result.label,
            "ok": result.outcome.ok,
            "claim": result.outcome.claim.value,
            "detail": result.outcome.detail,
            "evidence": result.evidence,
        }
        if result.outcome.ok:
            explanations = {e.task_id: e.model_dump(mode="json") for e in result.explanations}
            entry["tasks"] = [
                {
                    "id": row.task_id,
                    "project": row.project_id,
                    "name": row.name,
                    "start": row.start.isoformat(),
                    "finish": row.finish.isoformat(),
                    "duration": row.duration_wd,
                    "milestone": row.is_milestone,
                    "status": row.status.value,
                    "critical": row.critical,
                    "float": row.total_float_wd,
                    "shift": row.shift_wd,
                    "ref_start": row.reference_start.isoformat() if row.reference_start else None,
                    "ref_finish": row.reference_finish.isoformat() if row.reference_finish else None,
                    "deadline": _iso(tasks[row.task_id].hard_finish),
                    "due": _iso(tasks[row.task_id].due_date),
                    "bound": row.bound,
                    "demands": [
                        {
                            "resource": d.resource_id or row.bound.get(d.skill_id or ""),
                            "skill": d.skill_id,
                            "units": d.units,
                        }
                        for d in tasks[row.task_id].demands
                    ],
                    "why": explanations.get(row.task_id),
                }
                for row in result.tasks
            ]
            last = max((row.end_index for row in result.tasks), default=0) + 5
            load = resource_profiles(program, compiled, result.tasks)
            entry["load"] = {rid: values[:last] for rid, values in load.items()}
            entry["kpi"] = result.kpi.model_dump(mode="json") if result.kpi else None
            entry["violations"] = [v.model_dump(mode="json") for v in result.violations]
        scenarios.append(entry)
    edges = [
        {"src": e.src_task_id, "dst": e.dst_task_id, "type": e.type.value, "lag": e.lag_wd}
        for e in program.dependencies
        if e.hard
    ]
    years = sorted({d.year for d in compiled.axis.days})
    return {
        "program": {
            "id": program.program.id,
            "name": program.program.name,
            "status_date": program.program.planning_start.isoformat(),
            "provenance": program.provenance.kind.value,
        },
        "axis": axis_days,
        "availability": {rid: values for rid, values in compiled.availability.items()},
        "projects": projects,
        "resources": resources,
        "edges": edges,
        "scenarios": scenarios,
        "comparison": compare(plans),
        "conflicts": [c.as_dict() for c in analysis.conflicts] if analysis else [],
        "quality": [q.as_dict() for q in analysis.quality] if analysis else [],
        "witness": witness,
        "versions": {
            "okrplan": OKRPLAN_VERSION,
            "synaps": SYNAPS_COMMIT,
            "trl": ISO16290_TRL,
            "claim_level": CLAIM_LEVEL,
        },
        "provisional_years": [y for y in years if is_provisional(y)],
        "done_ids": [t.id for t in program.tasks if t.status is TaskStatus.DONE],
    }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def render_html(data: dict[str, Any], title: str | None = None) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    heading = html.escape(title or f"Сводный план программы ОКР — {data['program']['name']}")
    template = resources.files("synaps_okrplan").joinpath("report_template.html").read_text("utf-8")
    return template.replace("__TITLE__", heading).replace("__DATA__", payload)


def build_report(
    program: OKRProgram,
    plans: list[PlanResult],
    analysis: Analysis | None = None,
    witness: dict[str, Any] | None = None,
    title: str | None = None,
) -> str:
    return render_html(report_data(program, plans, analysis, witness), title)
