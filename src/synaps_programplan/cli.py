"""Command line: ``SynAPS-ProgramPlan <command>``.

Exit codes: 0 - accepted plan / check passed; 1 - no accepted plan, infeasible
or violations found; 2 - bad input or usage.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from synaps_programplan.checker import check_plan
from synaps_programplan.conflicts import analyze
from synaps_programplan.disrupt import Disruption, apply_disruption, roll_forward
from synaps_programplan.evidence import fingerprint
from synaps_programplan.explanations import attach_counterfactuals, explain, infeasibility_witness
from synaps_programplan.io import file_sha256, load_plan, load_program, save_plan, save_program
from synaps_programplan.io.excel import read_excel, write_template
from synaps_programplan.io.mspdi import ImportReport, read_mspdi
from synaps_programplan.merge import merge_projects, read_links_csv
from synaps_programplan.model import Provenance, ProvenanceKind
from synaps_programplan.montecarlo import simulate
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.report import build_report
from synaps_programplan.result import PlanResult, Severity
from synaps_programplan.scenarios import WhatIf, compare, run_scenarios
from synaps_programplan.synthetic import SyntheticSpec, generate
from synaps_programplan.versions import CLAIM_LEVEL, ISO16290_TRL, NAME, SYNAPS_COMMIT, VERSION


def _print(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n")


def _config(args: argparse.Namespace) -> SolveConfig:
    return SolveConfig(
        solver=args.solver,
        time_limit_s=args.time_limit,
        seed=args.seed,
        objective=args.objective,
        edge_mode=args.edge_mode,
    )


def _solve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--solver", choices=["cpsat", "greedy"], default="cpsat")
    parser.add_argument("--time-limit", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--objective", choices=["finish", "due"], default="finish")
    parser.add_argument("--edge-mode", choices=["native", "windows"], default="native")


def _summary(result: PlanResult) -> dict[str, Any]:
    kpi = result.kpi
    return {
        "scenario": result.scenario_id,
        "label": result.label,
        "ok": result.outcome.ok,
        "claim": result.outcome.claim.value,
        "detail": result.outcome.detail,
        "program_finish": kpi.program_finish if kpi else None,
        "late": kpi.late_count if kpi else None,
        "moved": kpi.moved_count if kpi else None,
        "hard_violations": result.outcome.domain_hard_violations,
        "plan_hash": result.evidence.get("plan_hash"),
    }


def cmd_synth(args: argparse.Namespace) -> int:
    program = generate(
        SyntheticSpec(
            projects=args.projects, seed=args.seed, deadline_slack=args.slack, infeasible=args.infeasible
        )
    )
    save_program(program, args.out)
    _print({"out": str(args.out), "tasks": len(program.tasks), "input_hash": fingerprint(program)})
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    if args.files[0].suffix.lower() == ".xlsx":
        if len(args.files) != 1:
            raise ValueError("Excel-импорт принимает одну книгу со всеми ОКР")
        program = read_excel(
            args.files[0],
            provenance=Provenance(
                kind=ProvenanceKind(args.provenance),
                source=args.files[0].name,
                source_file_hash=file_sha256(args.files[0]),
            ),
        )
        save_program(program, args.out)
        _print(
            {
                "out": str(args.out),
                "tasks": len(program.tasks),
                "projects": len(program.projects),
                "input_hash": fingerprint(program),
            }
        )
        return 0
    report = ImportReport()
    projects = []
    for index, path in enumerate(args.files):
        code = args.codes[index] if args.codes and index < len(args.codes) else f"okr{index + 1}"
        project = read_mspdi(path, code=code, report=report, deadline_hard=not args.soft_deadlines)
        project.source_hash = file_sha256(path)
        projects.append(project)
    links = read_links_csv(args.links) if args.links else []
    provenance = Provenance(
        kind=ProvenanceKind(args.provenance),
        source=", ".join(p.name for p in args.files),
        source_file_hash=fingerprint([p.source_hash for p in projects]),
    )
    program, merge = merge_projects(
        projects,
        program_id=args.program_id,
        name=args.name,
        status_date=args.status_date,
        links=links,
        provenance=provenance,
    )
    save_program(program, args.out)
    _print(
        {
            "out": str(args.out),
            "projects": len(projects),
            "tasks": len(program.tasks),
            "shared_resources": merge.shared_resources,
            "capacity_conflicts": merge.capacity_conflicts,
            "notes": report.notes + merge.notes,
        }
    )
    return 0


def cmd_template(args: argparse.Namespace) -> int:
    write_template(args.out)
    _print({"out": str(args.out), "sheets": 9})
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    analysis = analyze(program)
    _print(
        {
            "summary": analysis.summary(),
            "cpm_finish": analysis.cpm_finish,
            "milestone_risk": analysis.milestone_risk,
            "conflicts": [c.as_dict() for c in analysis.conflicts],
            "quality": [q.as_dict() for q in analysis.quality],
        }
    )
    return 0


def cmd_solve(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    result = plan(program, _config(args))
    if result.outcome.ok and args.explain:
        result.explanations = explain(program, result)
        attach_counterfactuals(program, result, top=args.counterfactuals)
    save_plan(result, args.out)
    _print(_summary(result))
    return 0 if result.outcome.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    result = load_plan(args.plan)
    violations = check_plan(program, result.tasks)
    hard = [v for v in violations if v.severity is Severity.HARD]
    _print({"hard": len(hard), "violations": [v.model_dump(mode="json") for v in violations[:200]]})
    return 1 if hard else 0


def cmd_explain(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    result = load_plan(args.plan)
    if not result.outcome.ok:
        _print({"error": "plan is not accepted (outcome.ok = false); nothing to explain"})
        return 1
    result.explanations = explain(program, result, limit=args.limit)
    attach_counterfactuals(program, result, top=args.counterfactuals)
    if args.out:
        save_plan(result, args.out)
    _print([e.model_dump(mode="json") for e in result.explanations])
    return 0


def cmd_witness(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    witness = infeasibility_witness(program, time_limit_s=args.time_limit)
    _print(witness)
    return 1 if witness.get("infeasible") else 0


def _what_ifs(spec: str | None) -> list[WhatIf]:
    if not spec:
        return []
    raw = json.loads(Path(spec).read_text(encoding="utf-8"))
    out = []
    for item in raw:
        out.append(
            WhatIf(
                label=item.get("label", "Что если"),
                add_capacity=item.get("add_capacity", {}),
                move_deadline={k: date.fromisoformat(v) for k, v in item.get("move_deadline", {}).items()},
                drop_projects=item.get("drop_projects", []),
                delay_task_wd=item.get("delay_task_wd", {}),
            )
        )
    return out


def cmd_scenarios(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    scenario_set = run_scenarios(program, _config(args), what_ifs=_what_ifs(args.what_if))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for result in scenario_set.plans:
        if result.outcome.ok and args.explain:
            result.explanations = explain(program, result)
        save_plan(result, args.out_dir / f"plan_{result.scenario_id}.json")
    _print(compare(scenario_set.plans))
    return 0 if any(p.outcome.ok for p in scenario_set.plans) else 1


def cmd_report(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    plans = [load_plan(path) for path in args.plans]
    witness = json.loads(args.witness.read_text(encoding="utf-8")) if args.witness else None
    html_text = build_report(program, plans, analyze(program), witness)
    args.out.write_text(html_text, encoding="utf-8")
    _print({"out": str(args.out), "plans": len(plans), "accepted": sum(p.outcome.ok for p in plans)})
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    previous = load_plan(args.plan)
    rolled = roll_forward(program, previous, args.status_date, freeze_wd=args.freeze_wd)
    disruption = Disruption(
        delay_wd=dict(_pair(item) for item in args.delay),
        unavailable=[_unavailable(item) for item in args.unavailable],
    )
    disrupted = apply_disruption(rolled, disruption)
    result = plan(
        disrupted, _config(args), scenario_id="repair", label=f"Перепланирование на {args.status_date}"
    )
    if result.outcome.ok:
        result.explanations = explain(disrupted, result)
    save_program(disrupted, args.out_program)
    save_plan(result, args.out)
    _print(_summary(result))
    return 0 if result.outcome.ok else 1


def _pair(text: str) -> tuple[str, int]:
    key, _, value = text.partition("=")
    return key, int(value)


def _unavailable(text: str) -> tuple[str, date, date]:
    resource, start, end = text.split(":")
    return resource, date.fromisoformat(start), date.fromisoformat(end)


def cmd_risk(args: argparse.Namespace) -> int:
    program = load_program(args.program)
    report = simulate(program, load_plan(args.plan), runs=args.runs, seed=args.seed)
    _print(report.as_dict())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """One command: synthetic program -> analysis -> scenarios -> explanations -> report."""
    out: Path = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    program = generate(SyntheticSpec(projects=args.projects, seed=args.seed))
    save_program(program, out / "program.json")
    analysis = analyze(program)
    config = SolveConfig(time_limit_s=args.time_limit, seed=args.seed)
    what_ifs = [
        WhatIf(label="второй стенд TS-1", add_capacity={"stand-1": 1}),
        WhatIf(label=f"без {program.projects[-1].code}", drop_projects=[program.projects[-1].id]),
    ]
    scenario_set = run_scenarios(program, config, what_ifs=what_ifs)
    for result in scenario_set.plans:
        if result.outcome.ok:
            result.explanations = explain(program, result)
        save_plan(result, out / f"plan_{result.scenario_id}.json")
    base = scenario_set.plans[0]
    if base.outcome.ok:
        attach_counterfactuals(program, base, top=args.counterfactuals, config=config)
        save_plan(base, out / f"plan_{base.scenario_id}.json")
    tight = generate(SyntheticSpec(projects=args.projects, seed=args.seed, infeasible=True))
    witness = infeasibility_witness(tight, time_limit_s=max(4, args.time_limit // 2))
    (out / "witness_infeasible.json").write_text(json.dumps(witness, ensure_ascii=False, indent=1), "utf-8")
    plans = [p for p in scenario_set.plans if p.scenario_id[0] != "E" or p.outcome.ok]
    (out / "report.html").write_text(build_report(program, plans, analysis), encoding="utf-8")
    (out / "report_infeasible.html").write_text(
        build_report(
            tight, [plan(tight, config, scenario_id="A", label="A · Сроки")], analyze(tight), witness
        ),
        encoding="utf-8",
    )
    _print(
        {
            "out_dir": str(out),
            "conflicts": analysis.summary(),
            "scenarios": compare(scenario_set.plans),
            "infeasible_witness": witness.get("text"),
        }
    )
    return 0 if base.outcome.ok else 1


def cmd_version(_: argparse.Namespace) -> int:
    _print(
        {
            "name": NAME,
            "version": VERSION,
            "synaps_commit": SYNAPS_COMMIT,
            "trl_iso16290": ISO16290_TRL,
            "claim_level": CLAIM_LEVEL,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="SynAPS-ProgramPlan", description="SynAPS-ProgramPlan: сводный план программы ОКР"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("synth", help="сгенерировать синтетическую программу")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--projects", type=int, default=4)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--slack", type=float, default=1.8)
    p.add_argument("--infeasible", action="store_true")
    p.set_defaults(func=cmd_synth)

    p = sub.add_parser("import", help="импорт MSPDI XML (несколько файлов) или одной книги Excel")
    p.add_argument("files", type=Path, nargs="+")
    p.add_argument("--codes", nargs="*")
    p.add_argument("--links", type=Path, help="CSV межпроектных связей")
    p.add_argument("--program-id", default="program")
    p.add_argument("--name", default="Программа ОКР")
    p.add_argument("--status-date", type=date.fromisoformat)
    p.add_argument("--soft-deadlines", action="store_true", help="Deadline из MS Project как плановый срок")
    p.add_argument("--provenance", choices=[k.value for k in ProvenanceKind], default="experiment")
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("template", help="пустая книга Excel для сводной программы")
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_template)

    for name, func, text in (
        ("analyze", cmd_analyze, "конфликты и качество исходных планов"),
        ("quality", cmd_analyze, "то же, что analyze"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("program", type=Path)
        p.set_defaults(func=func)

    p = sub.add_parser("solve", help="построить сводный план")
    p.add_argument("program", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--explain", action="store_true")
    p.add_argument("--counterfactuals", type=int, default=0)
    _solve_args(p)
    p.set_defaults(func=cmd_solve)

    p = sub.add_parser("check", help="независимая проверка плана")
    p.add_argument("program", type=Path)
    p.add_argument("plan", type=Path)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("explain", help="объяснить сдвиги принятого плана")
    p.add_argument("program", type=Path)
    p.add_argument("plan", type=Path)
    p.add_argument("--limit", type=int)
    p.add_argument("--counterfactuals", type=int, default=3)
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("witness", help="почему программа невыполнима")
    p.add_argument("program", type=Path)
    p.add_argument("--time-limit", type=int, default=8)
    p.set_defaults(func=cmd_witness)

    p = sub.add_parser("scenarios", help="варианты A-E")
    p.add_argument("program", type=Path)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--what-if", help="JSON со списком сценариев «что если»")
    p.add_argument("--explain", action="store_true")
    _solve_args(p)
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("report", help="HTML-отчёт: Гант, загрузка, сценарии, объяснения")
    p.add_argument("program", type=Path)
    p.add_argument("plans", type=Path, nargs="+")
    p.add_argument("--witness", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("repair", help="перепланирование на новую дату статуса")
    p.add_argument("program", type=Path)
    p.add_argument("plan", type=Path)
    p.add_argument("--status-date", type=date.fromisoformat, required=True)
    p.add_argument("--freeze-wd", type=int, default=10)
    p.add_argument("--delay", action="append", default=[], help="task_id=+рабочих дней")
    p.add_argument("--unavailable", action="append", default=[], help="resource_id:YYYY-MM-DD:YYYY-MM-DD")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--out-program", type=Path, required=True)
    _solve_args(p)
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("risk", help="P50/P80/P90 по принятому плану")
    p.add_argument("program", type=Path)
    p.add_argument("plan", type=Path)
    p.add_argument("--runs", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=cmd_risk)

    p = sub.add_parser("demo", help="демонстрация одной командой")
    p.add_argument("--out-dir", type=Path, default=Path("out/demo"))
    p.add_argument("--projects", type=int, default=4)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--time-limit", type=int, default=10)
    p.add_argument("--counterfactuals", type=int, default=3)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("version")
    p.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    if not args.verbose:
        logging.getLogger("synaps").setLevel(logging.WARNING)
    try:
        code: int = args.func(args)
    except (ValidationError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"SynAPS-ProgramPlan: input error: {exc}\n")
        return 2
    return code
