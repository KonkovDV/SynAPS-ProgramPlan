"""Stateless HTTP surface over the same functions as the command line.

A solve response is HTTP 200 only when ``outcome.ok`` is true. Anything else
is 409 with the verdict and no instruction to publish the dates. Storage,
roles and the operator journal live in the workbench (``workbench.py``,
``SynAPS-ProgramPlan serve``), not in this process.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from synaps_programplan.checker import check_plan
from synaps_programplan.conflicts import analyze
from synaps_programplan.edits import check_moves
from synaps_programplan.model import OKRProgram
from synaps_programplan.montecarlo import simulate
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.quality import quality_report
from synaps_programplan.result import PlanResult, Severity
from synaps_programplan.versions import CLAIM_LEVEL, ISO16290_TRL, NAME, SYNAPS_COMMIT, VERSION

app = FastAPI(title=NAME, version=VERSION)


class SolveRequest(BaseModel):
    program: dict[str, Any]
    solver: str = "greedy"
    time_limit_s: int = Field(default=8, ge=1, le=120)
    seed: int = 42


class RiskRequest(BaseModel):
    program: dict[str, Any]
    plan: dict[str, Any]
    runs: int = Field(default=100, ge=1, le=2000)
    seed: int = 42


def _program(payload: dict[str, Any]) -> OKRProgram:
    try:
        return OKRProgram.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/version")
def version() -> dict[str, object]:
    return {
        "name": NAME,
        "version": VERSION,
        "synaps_commit": SYNAPS_COMMIT,
        "trl_iso16290": ISO16290_TRL,
        "claim_level": CLAIM_LEVEL,
    }


@app.post("/analyze")
def analyze_program(payload: dict[str, Any]) -> dict[str, object]:
    program = _program(payload)
    found = analyze(program)
    return {
        "summary": found.summary(),
        "cpm_finish": found.cpm_finish.isoformat() if found.cpm_finish else None,
        "conflicts": [item.as_dict() for item in found.conflicts],
        "quality": quality_report(program),
    }


@app.post("/solve")
def solve_program(body: SolveRequest) -> dict[str, Any]:
    if body.solver not in {"cpsat", "greedy"}:
        raise HTTPException(status_code=422, detail="solver must be cpsat or greedy")
    program = _program(body.program)
    solver_name: Literal["cpsat", "greedy"] = "greedy" if body.solver == "greedy" else "cpsat"
    result = plan(program, SolveConfig(solver=solver_name, time_limit_s=body.time_limit_s, seed=body.seed))
    payload = result.model_dump(mode="json")
    if not result.outcome.ok:
        raise HTTPException(status_code=409, detail={"accepted": False, "result": payload})
    return {"accepted": True, "result": payload}


class CheckRequest(BaseModel):
    program: dict[str, Any]
    plan: dict[str, Any]
    moves: dict[str, date] = Field(default_factory=dict, max_length=5000)


@app.post("/check")
def check(body: CheckRequest) -> dict[str, Any]:
    """Independent check of a plan, optionally with manual moves (no solver)."""
    program = _program(body.program)
    try:
        accepted = PlanResult.model_validate(body.plan)
        if body.moves:
            return check_moves(program, accepted, body.moves)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    violations = check_plan(program, accepted.tasks)
    hard = sum(v.severity is Severity.HARD for v in violations)
    return {"ok": hard == 0, "hard": hard, "violations": [v.model_dump(mode="json") for v in violations]}


@app.post("/risk")
def risk(body: RiskRequest) -> dict[str, object]:
    program = _program(body.program)
    try:
        accepted = PlanResult.model_validate(body.plan)
        report = simulate(program, accepted, runs=body.runs, seed=body.seed)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return report.as_dict()
