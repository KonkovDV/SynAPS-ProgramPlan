"""Scale benchmark on synthetic OKR programs (no customer data).

Usage:
    python scripts/bench_scale.py [--sizes 8,16,32] [--tasks-per-stage 6] [--time-limit 60] [--out FILE]

Each size is the number of OKR projects; people and stands grow with the
program so that the load stays comparable. For every program the planner runs
the normal pipeline (compile -> solver -> kernel check -> independent domain
check) with CP-SAT and with the greedy dispatcher, and reports the verdict,
the program finish, the late milestones and the wall time.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Literal

from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.synthetic import SyntheticSpec, generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="8,16,32")
    parser.add_argument("--tasks-per-stage", type=int, default=6)
    parser.add_argument("--time-limit", type=int, default=60)
    parser.add_argument("--solvers", default="greedy,cpsat")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--deadline-slack", type=float, default=3.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    logging.getLogger("synaps").setLevel(logging.WARNING)
    rows: list[dict[str, Any]] = []
    for projects in (int(x) for x in args.sizes.split(",")):
        spec = SyntheticSpec(
            projects=projects,
            tasks_per_stage=args.tasks_per_stage,
            people_per_skill=max(2, projects // 2),
            stands=max(2, projects // 3),
            vacations=projects,
            maintenance=max(1, projects // 4),
            deadline_slack=args.deadline_slack,
            horizon_end=date(2031, 12, 26),
        )
        program = generate(spec)
        for solver in args.solvers.split(","):
            name: Literal["cpsat", "greedy"] = "greedy" if solver == "greedy" else "cpsat"
            config = SolveConfig(solver=name, time_limit_s=args.time_limit, seed=args.seed)
            started = time.perf_counter()
            result = plan(program, config, scenario_id=f"{projects}-{name}")
            elapsed = time.perf_counter() - started
            kpi = result.kpi
            row = {
                "projects": projects,
                "tasks": len(program.tasks),
                "links": len(program.dependencies),
                "resources": len(program.resources),
                "solver": name,
                "claim": result.outcome.claim.value,
                "ok": result.outcome.ok,
                "program_finish": kpi.program_finish.isoformat() if kpi and kpi.program_finish else None,
                "late_milestones": kpi.late_count if kpi else None,
                "tardiness_wd": kpi.tardiness_wd if kpi else None,
                "seconds": round(elapsed, 1),
                "time_limit_s": args.time_limit if name == "cpsat" else None,
            }
            rows.append(row)
            sys.stderr.write(json.dumps(row, ensure_ascii=False) + "\n")
    payload = {"seed": args.seed, "tasks_per_stage": args.tasks_per_stage, "rows": rows}
    if args.out:
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.write(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
