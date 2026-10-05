"""Public RCPSP and RCPSP/max benchmarks on PSPLIB instances (open data).

Usage:
    python scripts/bench_psplib.py DIR_WITH_SM [--opt J30_BKS.csv] [--limit 20] [--time-limit 10]
    python scripts/bench_psplib.py DIR_WITH_SCH --ref optimum.csv [--time-limit 10]

For every instance the planner runs the normal pipeline (compile -> CP-SAT ->
kernel check -> independent domain check) and reports the makespan, the
verdict, the resource-free critical-path lower bound and - when a reference
table is given - the gap to the published optimum or best known bound.

RCPSP/max (``.sch``) adds instances that are infeasible by design. They count
as answered only when the verdict is proven (``INFEASIBLE`` or a positive cycle
in the time lags); a plan accepted on such an instance, or a makespan below a
published lower bound, is a false claim and fails the run. PSPLIB files are not
shipped with the repository.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from synaps_programplan.cpm import cpm
from synaps_programplan.io.psplib import (
    InstanceInfeasible,
    Reference,
    load_sch,
    load_sm,
    read_max_reference,
    read_optimum,
)
from synaps_programplan.model import OKRProgram, difference_constraints
from synaps_programplan.planner import SolveConfig, plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--opt", type=Path, help="PSPLIB j30opt.sm or Solutions Update *_BKS.csv")
    parser.add_argument("--ref", type=Path, help="RCPSP/max optimum.csv (number, lb..ub or unsat)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--time-limit", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    logging.getLogger("synaps").setLevel(logging.WARNING)
    files = sorted(
        (p for p in args.directory.iterdir() if p.suffix.lower() in {".sm", ".sch"}),
        key=lambda p: [int(x) if x.isdigit() else x for x in _split(p.stem.lower())],
    )
    if args.limit:
        files = files[: args.limit]
    reference: dict[str, Reference] = {}
    if args.opt:
        reference = {k.lower(): Reference(v, v) for k, v in read_optimum(args.opt).items()}
    if args.ref:
        reference = read_max_reference(args.ref)
    config = SolveConfig(solver="cpsat", time_limit_s=args.time_limit, seed=args.seed, objective="finish")
    rows = [_run(path, config, reference.get(path.stem.lower())) for path in files]
    summary = _summary(rows)
    summary.update({"time_limit_s": args.time_limit, "seed": args.seed})
    payload = {"summary": summary, "rows": rows}
    if args.out:
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    return 0 if summary["false_claims"] == 0 and summary["unanswered"] == 0 else 1


def _run(path: Path, config: SolveConfig, ref: Reference | None) -> dict[str, Any]:
    started = time.perf_counter()
    row: dict[str, Any] = {"instance": path.stem}
    if ref is not None:
        row["reference"] = "infeasible" if ref.infeasible else (ref.lower, ref.upper)
    try:
        program = load_sch(path) if path.suffix.lower() == ".sch" else load_sm(path)
    except InstanceInfeasible as exc:
        row.update(claim="INFEASIBLE", proof=str(exc), ok=False)
        row["seconds"] = round(time.perf_counter() - started, 2)
        return _judge(row, ref)
    result = plan(program, config)
    row["seconds"] = round(time.perf_counter() - started, 2)
    row.update(claim=result.outcome.claim.value, ok=result.outcome.ok)
    if result.outcome.ok:
        makespan = result.task(_sink(program)).end_index
        bound = _cpm_bound(program)
        row.update(makespan=makespan, cpm_lb=bound)
        if bound:
            row["lb_dev_pct"] = round(100 * (makespan - bound) / bound, 2)
        if ref is not None and ref.upper:
            row["gap_pct"] = round(100 * (makespan - ref.upper) / ref.upper, 2)
    shown = row.get("makespan")
    sys.stderr.write(f"{path.stem}: {row['claim']} makespan={shown} ref={row.get('reference')}\n")
    return _judge(row, ref)


def _judge(row: dict[str, Any], ref: Reference | None) -> dict[str, Any]:
    """``false_claim``: accepted on a proven-infeasible instance, or below a published lower bound."""
    false_claim = False
    if ref is not None and ref.infeasible and row["ok"]:
        false_claim = True
    if ref is not None and ref.lower is not None and row.get("makespan") is not None:
        false_claim = false_claim or row["makespan"] < ref.lower
    if ref is not None and ref.lower is not None and row["claim"] == "INFEASIBLE":
        false_claim = True
    if ref is not None and row["claim"] == "OPTIMAL" and ref.lower == ref.upper and ref.upper is not None:
        false_claim = false_claim or row["makespan"] != ref.upper
    row["false_claim"] = false_claim
    return row


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [r for r in rows if r["ok"]]
    infeasible_ref = [r for r in rows if r.get("reference") == "infeasible"]

    def _exact(row: dict[str, Any]) -> bool:
        ref = row.get("reference")
        return isinstance(ref, tuple) and ref[0] == ref[1]

    with_optimum = [r for r in accepted if _exact(r)]
    open_ref = [r for r in accepted if isinstance(r.get("reference"), tuple) and not _exact(r)]
    gaps = [r["gap_pct"] for r in with_optimum]
    feasible_rows = [r for r in rows if r.get("reference") != "infeasible"]
    return {
        "instances": len(rows),
        "accepted": len(accepted),
        "optimal_claims": sum(r["claim"] == "OPTIMAL" for r in rows),
        "infeasible_claims": sum(r["claim"] == "INFEASIBLE" for r in rows),
        "reference_infeasible": len(infeasible_ref),
        "infeasible_proven": sum(r["claim"] == "INFEASIBLE" for r in infeasible_ref),
        "reference_feasible": len(feasible_rows),
        "feasible_found": sum(r["ok"] for r in feasible_rows),
        "with_optimum": len(gaps),
        "matched_optimum": sum(1 for g in gaps if g == 0),
        "mean_gap_pct": round(statistics.mean(gaps), 3) if gaps else None,
        "max_gap_pct": max(gaps) if gaps else None,
        "open_reference": len(open_ref),
        "open_at_or_below_best_known": sum(r["makespan"] <= r["reference"][1] for r in open_ref),
        "mean_lb_dev_pct": round(statistics.mean(r["lb_dev_pct"] for r in accepted if "lb_dev_pct" in r), 2)
        if any("lb_dev_pct" in r for r in accepted)
        else None,
        "unanswered": sum(not r["ok"] and r["claim"] != "INFEASIBLE" for r in rows),
        "false_claims": sum(r["false_claim"] for r in rows),
        "mean_seconds": round(statistics.mean(r["seconds"] for r in rows), 2) if rows else None,
        "max_seconds": max(r["seconds"] for r in rows) if rows else None,
    }


def _sink(program: OKRProgram) -> str:
    return next(t.id for t in program.tasks if t.name == "end")


def _cpm_bound(program: OKRProgram) -> int:
    durations = {t.id: t.duration_wd for t in program.tasks}
    lower = {t.id: (1 if t.duration_wd == 0 else 0) for t in program.tasks}
    return cpm(durations, difference_constraints(program), lower).finish


def _split(stem: str) -> list[str]:
    return re.split(r"(\d+)", stem)


if __name__ == "__main__":
    raise SystemExit(main())
