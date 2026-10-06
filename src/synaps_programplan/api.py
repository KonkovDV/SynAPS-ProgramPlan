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
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from synaps_programplan.auth import TokenStore
from synaps_programplan.checker import check_plan
from synaps_programplan.conflicts import analyze
from synaps_programplan.edits import check_moves
from synaps_programplan.model import OKRProgram
from synaps_programplan.montecarlo import simulate
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.publish import attestation_error, require_attestation
from synaps_programplan.quality import quality_report
from synaps_programplan.result import PlanResult, Severity
from synaps_programplan.versions import CLAIM_LEVEL, ISO16290_TRL, NAME, SYNAPS_COMMIT, VERSION

app = FastAPI(title=NAME, version=VERSION)
MAX_BODY_BYTES = 32 * 1024 * 1024
_LOCAL_CLIENTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _declared_body_too_large(request: Request) -> JSONResponse | None:
    declared = request.headers.get("content-length")
    if declared is None:
        return None
    try:
        size = int(declared)
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "content-length is not an integer"})
    if size > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "request body is larger than 32 MiB"})
    return None


@app.middleware("http")
async def refuse_oversized_or_anonymous_remote(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    """32 MiB is the body cap. Without tokens only a local client is answered.

    ``Content-Length`` above the cap is refused before the body is read.
    ``request.body()`` is what Starlette replays to the handler; ``stream()``
    would leave that handler with an empty body.
    """
    refused = _declared_body_too_large(request)
    if refused is not None:
        return refused
    if len(await request.body()) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "request body is larger than 32 MiB"})
    host = request.client.host if request.client else ""
    tokens = TokenStore.from_env()
    if tokens.enabled:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else None
        if tokens.authenticate(token) is None:
            return JSONResponse(status_code=401, content={"detail": "token rejected"})
    elif host not in _LOCAL_CLIENTS:
        return JSONResponse(status_code=401, content={"detail": "remote API requires a token"})
    return await call_next(request)


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
    """Independent check of a plan, optionally with manual moves (no solver).

    HTTP 200 only when the checker found no hard violation. A plan that fails
    is 409, the same rule as ``/solve``: a client that publishes on status 200
    cannot publish a broken plan.
    """
    program = _program(body.program)
    try:
        accepted = PlanResult.model_validate(body.plan)
        rejected = attestation_error(program, accepted)
        if rejected:
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "accepted": False,
                    "claim": accepted.outcome.claim.value,
                    "detail": rejected,
                },
            )
        if body.moves:
            payload = check_moves(program, accepted, body.moves)
        else:
            violations = check_plan(program, accepted.tasks)
            hard = sum(v.severity is Severity.HARD for v in violations)
            payload = {
                "ok": hard == 0,
                "hard": hard,
                "violations": [v.model_dump(mode="json") for v in violations],
            }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not payload["ok"]:
        raise HTTPException(status_code=409, detail=payload)
    return payload


@app.post("/risk")
def risk(body: RiskRequest) -> dict[str, object]:
    program = _program(body.program)
    try:
        accepted = PlanResult.model_validate(body.plan)
        require_attestation(program, accepted)
        report = simulate(program, accepted, runs=body.runs, seed=body.seed)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return report.as_dict()
