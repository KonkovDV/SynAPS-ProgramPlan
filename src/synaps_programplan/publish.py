"""The single gate before dates leave the process.

A plan is publishable only when the solver marked it accepted, the stored
program hash is this program, and the stored plan hash still matches the
dates and the verdict. Checkers, reports, export and HTTP all call this
instead of trusting ``outcome.ok`` alone.
"""

from __future__ import annotations

from synaps_programplan.evidence import fingerprint
from synaps_programplan.model import OKRProgram
from synaps_programplan.planner import plan_hash
from synaps_programplan.result import PlanResult


def attestation_error(program: OKRProgram, plan: PlanResult) -> str | None:
    """None when the plan may be shown. Otherwise a stable reason string."""
    if not plan.outcome.ok:
        claim = plan.outcome.claim.value
        return f"plan is not accepted (outcome.ok = false, claim {claim})"
    stamped_input = plan.evidence.get("input_hash")
    if stamped_input != fingerprint(program):
        return "plan evidence.input_hash does not match the program"
    stamped_plan = plan.evidence.get("plan_hash")
    if stamped_plan != plan_hash(plan):
        return "plan evidence.plan_hash does not match the plan"
    return None


def require_attestation(program: OKRProgram, plan: PlanResult) -> None:
    error = attestation_error(program, plan)
    if error:
        raise ValueError(error)
