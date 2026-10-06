"""Planner workbench (requirement F5): the report as a working tool.

``SynAPS-ProgramPlan serve`` keeps one program and its accepted variants in
memory and serves the same HTML report with an edit mode on top:

* drag a task (or move it with the keyboard) - the independent checker judges
  the edited plan on the server, no solver involved;
* "re-plan with edits" pins the moved tasks and re-solves the rest, by default
  with as few changes to the edited variant as possible; the new variant is
  shown only if it passes the same ``outcome.ok`` gate;
* accept / reject a variant with a reason - written to a hash-chained journal.

Roles come from ``SYNAPS_PROGRAMPLAN_TOKENS`` (see ``auth``). Without tokens
the server only answers on loopback host names.
"""

from __future__ import annotations

import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from synaps_programplan.auth import Principal, TokenStore, proxy_principal
from synaps_programplan.checker import check_plan
from synaps_programplan.conflicts import Analysis, analyze
from synaps_programplan.edits import ReplanMode, check_moves, repair_with_moves
from synaps_programplan.evidence import fingerprint
from synaps_programplan.io import load_plan, save_plan
from synaps_programplan.io.mspdi import write_plan_mspdi
from synaps_programplan.journal import append_decision, read_journal, verify_journal, witness_path
from synaps_programplan.model import OKRProgram
from synaps_programplan.montecarlo import RiskResult
from synaps_programplan.planner import SolveConfig, plan_hash
from synaps_programplan.report import render_html, report_data
from synaps_programplan.result import PlanResult, Severity
from synaps_programplan.versions import NAME, VERSION

LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1", "testserver"]


def exposure_refusal(host: str, *, authenticated: bool, tls: bool) -> str | None:
    """Why ``serve`` must refuse this bind, or None when it may listen.

    Loopback needs nothing else. Any other address needs roles and TLS of its
    own: a TLS-terminating proxy should keep this process on loopback.
    """
    if host in LOOPBACK_HOSTS:
        return None
    if not authenticated:
        return "without SYNAPS_PROGRAMPLAN_TOKENS the workbench listens on loopback only"
    if not tls:
        return "a non-loopback address requires --tls-cert and --tls-key"
    return None


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class Workbench:
    program: OKRProgram
    plans: list[PlanResult]
    journal: Path
    save_dir: Path | None = None
    config: SolveConfig = field(default_factory=SolveConfig)
    witness: dict[str, Any] | None = None
    risk: RiskResult | None = None
    tokens: TokenStore = field(default_factory=TokenStore.from_env)
    _analysis: Analysis | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _edits: int = 0
    restored: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.journal.exists() or witness_path(self.journal).exists():
            check = verify_journal(self.journal)
            if not check.ok:
                raise ValueError(f"decision journal failed verification: {check.reason}")
        records = read_journal(self.journal)
        seen = [r.get("scenario_id") or "" for r in records] + [p.scenario_id for p in self.plans]
        self._edits = max([0, *(_edit_number(s) for s in seen)])
        if self.save_dir is not None and records:
            self._restore(records)

    def _restore(self, records: list[dict[str, Any]]) -> None:
        """Bring back re-planned variants of a previous session, but only those the
        journal vouches for: same input, same recomputed plan hash, no hard violation."""
        assert self.save_dir is not None
        loaded = {p.scenario_id for p in self.plans}
        input_hash = fingerprint(self.program)
        for record in records:
            scenario_id = record.get("scenario_id") or ""
            path = self.save_dir / f"plan_{scenario_id}.json"
            if (
                record.get("action") != "repair"
                or not record.get("plan_hash")
                or record.get("input_hash") != input_hash
                or scenario_id in loaded
                or not path.exists()
            ):
                continue
            result = load_plan(path)
            if not result.outcome.ok or plan_hash(result) != record["plan_hash"]:
                continue
            if any(v.severity is Severity.HARD for v in check_plan(self.program, result.tasks)):
                continue
            self.plans.append(result)
            self.restored.append(scenario_id)
            loaded.add(scenario_id)

    @property
    def analysis(self) -> Analysis:
        if self._analysis is None:
            self._analysis = analyze(self.program)
        return self._analysis

    def find(self, scenario_id: str) -> PlanResult:
        for result in self.plans:
            if result.scenario_id == scenario_id:
                return result
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id!r}")

    def accepted(self, scenario_id: str) -> PlanResult:
        result = self.find(scenario_id)
        if not result.outcome.ok:
            raise HTTPException(status_code=409, detail="scenario has no accepted plan (outcome.ok = false)")
        return result

    def data(self, principal: Principal) -> dict[str, Any]:
        payload = report_data(self.program, self.plans, self.analysis, self.witness, self.risk)
        payload["workbench"] = {
            "auth": self.tokens.enabled,
            "user": principal.user,
            "role": principal.role.value,
            "may": sorted(a for a in ("view", "check", "decide", "repair") if principal.may(a)),
        }
        return payload


def _edit_number(scenario_id: str) -> int:
    match = re.fullmatch(r"R(\d+)", scenario_id)
    return int(match.group(1)) if match else 0


def _principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
    bench: Workbench = request.app.state.bench
    # A user header from a proxy counts only together with the shared secret.
    # A secret that does not match is a rejection, not a fall-through to local mode.
    if request.headers.get("x-synaps-proxy-secret") is not None:
        proxied = proxy_principal(
            request.headers.get("x-synaps-proxy-secret"),
            request.headers.get("x-remote-user"),
            request.headers.get("x-remote-role"),
        )
        if proxied is None:
            raise HTTPException(status_code=401, detail="proxy authentication rejected")
        return proxied
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    found = bench.tokens.authenticate(token)
    if found is None:
        raise HTTPException(status_code=401, detail="token required", headers={"WWW-Authenticate": "Bearer"})
    return found


User = Annotated[Principal, Depends(_principal)]


def _allowed(who: Principal, action: str) -> Principal:
    if not who.may(action):
        raise HTTPException(status_code=403, detail=f"role {who.role.value} may not {action}")
    return who


class MovesRequest(BaseModel):
    scenario: str
    moves: dict[str, date] = Field(max_length=5000)


class RepairRequest(MovesRequest):
    mode: ReplanMode = "stable"


class DecisionRequest(BaseModel):
    scenario: str
    action: Literal["accept", "reject"]
    reason: str = Field(min_length=3, max_length=2000)


def create_app(bench: Workbench) -> FastAPI:
    app = FastAPI(title=f"{NAME} workbench", version=VERSION, docs_url=None, redoc_url=None)
    app.state.bench = bench
    if not bench.tokens.enabled:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOOPBACK_HOSTS)

    @app.middleware("http")
    async def _headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        shell = {"workbench": {"auth": bench.tokens.enabled, "remote": True}}
        return render_html(shell, title=f"Сводный план программы ОКР — {bench.program.program.name}")

    @app.get("/api/data")
    def data(who: User) -> dict[str, Any]:
        _allowed(who, "view")
        with bench._lock:
            return bench.data(who)

    @app.get("/api/whoami")
    def whoami(who: User) -> dict[str, Any]:
        return {"user": who.user, "role": who.role.value, "auth": bench.tokens.enabled}

    @app.post("/api/check")
    def check(body: MovesRequest, who: User) -> dict[str, Any]:
        _allowed(who, "check")
        base = bench.accepted(body.scenario)
        try:
            return check_moves(bench.program, base, body.moves)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/repair")
    def repair(body: RepairRequest, who: User) -> dict[str, Any]:
        _allowed(who, "repair")
        if not body.moves:
            raise HTTPException(status_code=422, detail="no edits to re-plan with")
        with bench._lock:
            base = bench.accepted(body.scenario)
            bench._edits += 1
            scenario_id = f"R{bench._edits}"
            label = f"{scenario_id} · правка {base.scenario_id}: закреплено {len(body.moves)}"
            try:
                result = repair_with_moves(
                    bench.program,
                    base,
                    body.moves,
                    bench.config,
                    scenario_id=scenario_id,
                    label=label,
                    mode=body.mode,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            replan = result.metadata.get("replan", {})
            if result.outcome.ok:
                bench.plans.append(result)
                if bench.save_dir is not None:
                    bench.save_dir.mkdir(parents=True, exist_ok=True)
                    save_plan(result, bench.save_dir / f"plan_{scenario_id}.json")
            append_decision(
                bench.journal,
                action="repair",
                user=who.user,
                role=who.role.value,
                scenario_id=scenario_id if result.outcome.ok else base.scenario_id,
                plan_hash=result.evidence.get("plan_hash") if result.outcome.ok else None,
                input_hash=fingerprint(bench.program),
                reason=f"правка варианта {base.scenario_id}",
                details={
                    "base": base.scenario_id,
                    "moves": {k: v.isoformat() for k, v in sorted(body.moves.items())},
                    "claim": result.outcome.claim.value,
                    "mode": replan.get("mode", body.mode),
                    "requested_mode": body.mode,
                    "churn": replan.get("churn"),
                },
            )
        summary = {
            "scenario": scenario_id,
            "ok": result.outcome.ok,
            "claim": result.outcome.claim.value,
            "detail": result.outcome.detail,
            "mode": replan.get("mode", body.mode),
            "requested_mode": body.mode,
            "churn": replan.get("churn"),
        }
        if not result.outcome.ok:
            raise HTTPException(status_code=409, detail=summary)
        return summary

    @app.get("/api/decisions")
    def decisions(who: User) -> dict[str, Any]:
        _allowed(who, "view")
        return {"records": read_journal(bench.journal), "integrity": verify_journal(bench.journal).as_dict()}

    @app.post("/api/decisions")
    def decide(body: DecisionRequest, who: User) -> dict[str, Any]:
        _allowed(who, "decide")
        with bench._lock:
            result = bench.accepted(body.scenario) if body.action == "accept" else bench.find(body.scenario)
            return append_decision(
                bench.journal,
                action=body.action,
                user=who.user,
                role=who.role.value,
                scenario_id=result.scenario_id,
                plan_hash=result.evidence.get("plan_hash"),
                input_hash=result.evidence.get("input_hash") or fingerprint(bench.program),
                reason=body.reason,
                details={"claim": result.outcome.claim.value},
            )

    @app.get("/api/plans/{scenario_id}")
    def plan_json(scenario_id: str, who: User) -> Response:
        _allowed(who, "view")
        result = bench.accepted(scenario_id)
        return Response(
            result.model_dump_json(indent=1),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="plan_{scenario_id}.json"'},
        )

    @app.get("/api/plans/{scenario_id}/mspdi")
    def plan_mspdi(scenario_id: str, who: User) -> Response:
        _allowed(who, "view")
        result = bench.accepted(scenario_id)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.xml"
            write_plan_mspdi(bench.program, result, path)
            body = path.read_bytes()
        return Response(
            body,
            media_type="application/xml",
            headers={"Content-Disposition": f'attachment; filename="plan_{scenario_id}.xml"'},
        )

    return app
