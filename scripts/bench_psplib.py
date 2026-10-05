"""Public RCPSP benchmark on PSPLIB single-mode instances (open data).

Usage:
    python scripts/bench_psplib.py DIR_WITH_SM [--opt j30opt.sm] [--limit 20] [--time-limit 10]

For every instance the planner runs the normal pipeline (compile -> CP-SAT ->
kernel check -> independent domain check) and reports the makespan, the
verdict, the resource-free critical-path lower bound and - when an optimum
table is given - the gap to the published optimum. PSPLIB files are not
shipped with the repository: download them from the PSPLIB site.
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

from synaps_programplan.cpm import cpm
from synaps_programplan.io.psplib import load_sm, read_optimum
from synaps_programplan.model import difference_constraints
from synaps_programplan.planner import SolveConfig, plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--opt", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--time-limit", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    logging.getLogger("synaps").setLevel(logging.WARNING)
    files = sorted(
        args.directory.glob("*.sm"), key=lambda p: [int(x) if x.isdigit() else x for x in _split(p.stem)]
    )
    if args.limit:
        files = files[: args.limit]
    optimum = read_optimum(args.opt) if args.opt else {}
    config = SolveConfig(solver="cpsat", time_limit_s=args.time_limit, seed=args.seed, objective="finish")
    rows = []
    for path in files:
        program = load_sm(path)
        durations = {t.id: t.duration_wd for t in program.tasks}
        lower = {t.id: (1 if t.duration_wd == 0 else 0) for t in program.tasks}
        bound = cpm(durations, difference_constraints(program), lower).finish
        started = time.perf_counter()
        result = plan(program, config)
        elapsed = time.perf_counter() - started
        sink = next(t.id for t in program.tasks if t.name == "end")
        makespan = result.task(sink).end_index if result.outcome.ok else None
        best = optimum.get(path.stem)
        rows.append(
            {
                "instance": path.stem,
                "claim": result.outcome.claim.value,
                "ok": result.outcome.ok,
                "makespan": makespan,
                "cpm_lb": bound,
                "optimum": best,
                "gap_pct": round(100 * (makespan - best) / best, 2)
                if makespan is not None and best
                else None,
                "lb_dev_pct": round(100 * (makespan - bound) / bound, 2)
                if makespan is not None and bound
                else None,
                "seconds": round(elapsed, 2),
            }
        )
        sys.stderr.write(f"{path.stem}: {rows[-1]['claim']} makespan={makespan} opt={best}\n")
    accepted = [r for r in rows if r["ok"]]
    gaps = [r["gap_pct"] for r in accepted if r["gap_pct"] is not None]
    summary = {
        "instances": len(rows),
        "accepted": len(accepted),
        "optimal_claims": sum(r["claim"] == "OPTIMAL" for r in rows),
        "matched_optimum": sum(1 for g in gaps if g == 0),
        "with_optimum": len(gaps),
        "mean_gap_pct": round(statistics.mean(gaps), 3) if gaps else None,
        "max_gap_pct": max(gaps) if gaps else None,
        "mean_lb_dev_pct": round(statistics.mean(r["lb_dev_pct"] for r in accepted), 2) if accepted else None,
        "mean_seconds": round(statistics.mean(r["seconds"] for r in rows), 2) if rows else None,
        "time_limit_s": args.time_limit,
        "seed": args.seed,
    }
    payload = {"summary": summary, "rows": rows}
    if args.out:
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    return 0 if len(accepted) == len(rows) else 1


def _split(stem: str) -> list[str]:
    return re.split(r"(\d+)", stem)


if __name__ == "__main__":
    raise SystemExit(main())
