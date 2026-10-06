"""Time and memory of the first feasible plan as programs grow.

Greedy only: it is the fast feasible plan. CP-SAT is not started here.
A size that exceeds ``--limit-s`` is recorded and the larger sizes are not
started. Nothing is claimed beyond the rows this script writes.

    python scripts/bench_boundary.py --projects 167,334,667 --tasks-per-stage 4 --limit-s 180
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from synaps_programplan.isolate import process_rss_mb
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.synthetic import SyntheticSpec, generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--projects", default="167,334,667")
    parser.add_argument("--tasks-per-stage", type=int, default=4)
    parser.add_argument("--limit-s", type=float, default=180)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/scale_boundary.json"))
    args = parser.parse_args(argv)
    logging.getLogger("synaps").setLevel(logging.WARNING)
    rows: list[dict[str, Any]] = []
    for projects in (int(item) for item in args.projects.split(",")):
        row = _one(projects, args.tasks_per_stage, args.seed, args.limit_s)
        rows.append(row)
        sys.stderr.write(json.dumps(row, ensure_ascii=False) + "\n")
        _write(args.out, args.seed, rows)
        if not row["ok"] or row["seconds"] > args.limit_s:
            break
    return 0


def _one(projects: int, tasks_per_stage: int, seed: int, limit_s: float) -> dict[str, Any]:
    spec = SyntheticSpec(
        projects=projects,
        tasks_per_stage=tasks_per_stage,
        people_per_skill=max(2, projects // 2),
        stands=max(2, projects // 3),
        vacations=0,
        maintenance=0,
        deadline_slack=3.0,
        seed=seed,
        horizon_end=date(2032, 12, 30),
    )
    started = time.perf_counter()
    program = generate(spec)
    generated = time.perf_counter() - started
    peak = {"mb": process_rss_mb(os.getpid()) or 0.0}
    stop = False

    def sample() -> None:
        while not stop:
            rss = process_rss_mb(os.getpid())
            if rss is not None and rss > peak["mb"]:
                peak["mb"] = rss
            time.sleep(0.2)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    solve_started = time.perf_counter()
    try:
        result = plan(program, SolveConfig(solver="greedy", seed=seed), scenario_id=f"n{projects}")
        claim = result.outcome.claim.value
        ok = result.outcome.ok
        detail = "" if ok else (result.outcome.detail or "")[:300]
    except ValidationError as exc:
        claim = "ValidationError"
        ok = False
        detail = "; ".join(str(err.get("msg", "")) for err in exc.errors())[:300]
    except Exception as exc:  # a size that does not fit is a measured boundary
        claim = type(exc).__name__
        ok = False
        detail = str(exc).splitlines()[0][:300]
    seconds = time.perf_counter() - solve_started
    stop = True
    thread.join(1)
    return {
        "projects": projects,
        "tasks": len(program.tasks),
        "links": len(program.dependencies),
        "resources": len(program.resources),
        "solver": "greedy",
        "claim": claim,
        "ok": ok,
        "detail": detail,
        "generate_seconds": round(generated, 1),
        "seconds": round(seconds, 1),
        "rss_mb": round(peak["mb"], 1),
        "limit_s": limit_s,
        "stopped": seconds > limit_s or not ok,
    }


def _write(path: Path, seed: int, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed": seed,
        "solver": "greedy",
        "note": (
            "Люди и стенды растут вместе с числом проектов, как в scripts/bench_scale.py. "
            "Размер, на котором лимит времени превышен или план не найден, дальше не продолжается. "
            "Отдельно: 5010 работ, 2 человека на компетенцию и 4 стенда — "
            "жадный план не найден, 50,8 с, 180 МБ."
        ),
        "rows": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
