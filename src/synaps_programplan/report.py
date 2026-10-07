"""Self-contained interactive HTML report (requirement F5).

One file, no CDN, no network: embedded JSON + vanilla JS/SVG. Only plans with
``outcome.ok`` get a Gantt chart; rejected or infeasible scenarios appear in
the comparison table with their verdict and no dates.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from datetime import date
from importlib import resources
from typing import Any

from synaps_programplan.calendar import is_provisional
from synaps_programplan.compiler import compile_program
from synaps_programplan.conflicts import Analysis
from synaps_programplan.model import OKRProgram, TaskStatus
from synaps_programplan.montecarlo import RiskResult
from synaps_programplan.planner import resource_profiles
from synaps_programplan.publish import attestation_error
from synaps_programplan.result import PlanResult
from synaps_programplan.scenarios import compare
from synaps_programplan.versions import CLAIM_LEVEL, ISO16290_TRL, NAME, SYNAPS_COMMIT, VERSION


def report_data(
    program: OKRProgram,
    plans: list[PlanResult],
    analysis: Analysis | None = None,
    witness: dict[str, Any] | None = None,
    risk: RiskResult | None = None,
) -> dict[str, Any]:
    compiled = compile_program(program)
    axis_days = [d.isoformat() for d in compiled.axis.days]
    projects = [
        {
            "id": p.id,
            "code": p.code,
            "name": p.name,
            "enterprise": p.enterprise,
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
        publishable = attestation_error(program, result) is None
        entry["ok"] = publishable
        if publishable:
            explanations = {e.task_id: e.model_dump(mode="json") for e in result.explanations}
            entry["tasks"] = [
                {
                    "id": row.task_id,
                    "project": row.project_id,
                    "wbs": tasks[row.task_id].wbs_id,
                    "name": row.name,
                    "article": _article_code(program, tasks[row.task_id].test_article_id),
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
                    **_progress(program, compiled.axis, tasks[row.task_id], row),
                    "deadline": _iso(tasks[row.task_id].hard_finish),
                    "due": _iso(tasks[row.task_id].due_date),
                    "bound": row.bound,
                    "mode": row.mode_code,
                    "demands": [
                        {
                            "resource": d.resource_id or row.bound.get(d.skill_id or ""),
                            "skill": d.skill_id,
                            "units": d.units,
                        }
                        for d in tasks[row.task_id].demands_for(row.mode_code)
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
        {
            "src": e.src_task_id,
            "dst": e.dst_task_id,
            "type": e.type.value,
            "lag": e.lag_wd,
            "max_lag": e.max_lag_wd,
        }
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
        "wbs": [
            {"id": node.id, "project": node.project_id, "code": node.code, "name": node.name}
            for node in program.wbs
        ],
        "resources": resources,
        "edges": edges,
        "scenarios": scenarios,
        "comparison": compare(plans),
        "conflicts": [c.as_dict() for c in analysis.conflicts] if analysis else [],
        "quality": [q.as_dict() for q in analysis.quality] if analysis else [],
        "witness": witness,
        "versions": {
            "name": NAME,
            "version": VERSION,
            "synaps": SYNAPS_COMMIT,
            "trl": ISO16290_TRL,
            "claim_level": CLAIM_LEVEL,
        },
        "provisional_years": [y for y in years if is_provisional(y)],
        "done_ids": [t.id for t in program.tasks if t.status is TaskStatus.DONE],
        "risk": _risk(risk, tasks) if risk else None,
    }


def _risk(risk: RiskResult, tasks: dict[str, Any]) -> dict[str, Any]:
    payload = risk.as_dict()
    critical = sorted(risk.criticality.items(), key=lambda item: (-item[1], item[0]))
    payload["criticality_top"] = [
        {
            "task_id": task_id,
            "name": tasks[task_id].name,
            "project": tasks[task_id].project_id,
            "index": share,
        }
        for task_id, share in critical[:15]
        if share > 0 and task_id in tasks
    ]
    payload.pop("criticality")
    payload["milestone_risk"] = [
        {**item.as_dict(), "project": tasks[item.task_id].project_id if item.task_id in tasks else ""}
        for item in risk.milestone_risk
    ]
    return payload


def _article_code(program: OKRProgram, article_id: str | None) -> str | None:
    if article_id is None:
        return None
    for article in program.articles:
        if article.id == article_id:
            return article.code
    return article_id


def _slip(axis: Any, left: date | None, right: date | None) -> int | None:
    if left is None or right is None:
        return None
    return int(axis.boundary_after(right) - axis.boundary_after(left))


def _progress(program: OKRProgram, axis: Any, source: Any, row: Any) -> dict[str, Any]:
    """Baseline, actual and forecast dates, plus the slip of the finish against each."""
    status_date = program.program.planning_start
    baseline_start = source.baseline_start or row.reference_start
    baseline_finish = source.baseline_finish or row.reference_finish
    if source.status is TaskStatus.DONE:
        forecast_start = forecast_finish = None
    elif source.status is TaskStatus.IN_PROGRESS:
        forecast_start, forecast_finish = status_date, row.finish
    else:
        forecast_start, forecast_finish = row.start, row.finish
    anchor = source.planned_finish or baseline_finish
    fact_slip = None
    if source.status is TaskStatus.DONE and source.actual_finish is not None:
        fact_slip = _slip(axis, anchor, source.actual_finish)
    elif source.status is TaskStatus.IN_PROGRESS:
        fact_slip = _slip(axis, anchor, row.finish)
    return {
        "percent": source.progress_percent(),
        "baseline_start": _iso(baseline_start),
        "baseline_finish": _iso(baseline_finish),
        "actual_start": _iso(source.actual_start),
        "actual_finish": _iso(source.actual_finish),
        "forecast_start": _iso(forecast_start),
        "forecast_finish": _iso(forecast_finish),
        "baseline_slip_wd": _slip(axis, baseline_finish, row.finish),
        "fact_slip_wd": fact_slip,
    }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _json_for_script(data: dict[str, Any]) -> str:
    """JSON that cannot close the surrounding script block.

    The replacement is case-blind: every ``<``, ``>`` and ``&`` becomes a
    Unicode escape, as do the line separators U+2028 and U+2029.
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def _script_hash(body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def _script_src(page: str, nonce: str | None) -> tuple[str, str]:
    """CSP source for the inline scripts, and the page with those scripts marked.

    A hash is of the exact script text. A hash and ``'unsafe-inline'`` together
    make the browser ignore ``'unsafe-inline'``, so styles stay on
    ``'unsafe-inline'`` alone: the page sets colours from script.
    """
    if nonce is not None:
        if re.fullmatch(r"[0-9a-f]+", nonce) is None:
            raise ValueError("CSP nonce must be hexadecimal")
        stamped = _SCRIPT_BLOCK.sub(
            lambda match: f'<script nonce="{nonce}"{match.group(1)}>{match.group(2)}</script>',
            page,
        )
        return f"'nonce-{nonce}'", stamped
    bodies = _SCRIPT_BLOCK.findall(page)
    if not bodies:
        raise RuntimeError("report has no script to pin")
    sources = " ".join(f"'{_script_hash(body)}'" for _attrs, body in bodies)
    return sources, page


def render_html(data: dict[str, Any], title: str | None = None, *, nonce: str | None = None) -> str:
    payload = _json_for_script(data)
    heading = html.escape(title or f"Сводный план программы ОКР — {data['program']['name']}")
    template = resources.files("synaps_programplan").joinpath("report_template.html").read_text("utf-8")
    page = template.replace("__TITLE__", heading).replace("__DATA__", payload)
    script_src, page = _script_src(page, nonce)
    if "__SCRIPT_SRC__" not in page:
        raise RuntimeError("report template has no script policy placeholder")
    return page.replace("__SCRIPT_SRC__", script_src)


def build_report(
    program: OKRProgram,
    plans: list[PlanResult],
    analysis: Analysis | None = None,
    witness: dict[str, Any] | None = None,
    title: str | None = None,
    risk: RiskResult | None = None,
) -> str:
    return render_html(report_data(program, plans, analysis, witness, risk), title)
