"""Measure the record behind the documents, and fail when they drift apart.

``evidence.json`` is extracted from the demonstration directory and from the
test suite. CI does not re-solve that directory: the Deadlines alternative is
limited to 8 seconds, so another machine can produce another plan.

    python scripts/build_evidence.py --demo-dir out/demo
    python scripts/build_evidence.py --check
    python scripts/build_evidence.py --check --demo-dir out/demo
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence.json"

COUNT_PATTERNS: dict[str, list[str]] = {
    "README.md": [r"Автоматических тестов — (\d+)\."],
    "README.en.md": [r"Automated tests: (\d+)\."],
    "docs/quality-assurance.md": [
        r"Набор `tests/` \((\d+) тест",
        r"Python 3\.12: (\d+) тест",
        r"5 010 работах, (\d+) тест",
    ],
    "docs/gap-register.md": [r"(\d+) тест"],
    "docs/pilot-and-roadmap.md": [r"(\d+) автоматическ"],
}

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def ru_date(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{day}.{month}.{year}"


def en_date(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{int(day)} {_MONTHS[int(month) - 1]} {year}"


def pattern_gaps(text: str, patterns: list[str], expected: int) -> list[str]:
    gaps: list[str] = []
    for pattern in patterns:
        found = [int(item) for item in re.findall(pattern, text)]
        if found != [expected] * len(found) or not found:
            gaps.append(f"expected {expected} from {pattern}, found {found or 'nothing'}")
    return gaps


def collected_tests() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    blob = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        raise SystemExit(blob)
    match = re.search(r"(\d+) tests collected", blob)
    if match is None:
        raise SystemExit(blob)
    return int(match.group(1))


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _java() -> str | None:
    if shutil.which("java") is None:
        return None
    proc = subprocess.run(
        ["java", "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = (proc.stderr or proc.stdout).splitlines()
    return lines[0] if lines else None


def environment() -> dict[str, Any]:
    from synaps_programplan.versions import SYNAPS_COMMIT, VERSION

    return {
        "measured_at": date.today().isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "product_version": VERSION,
        "synaps_commit": SYNAPS_COMMIT,
        "ortools": _version("ortools"),
        "java": _java(),
        "mpxj": _version("mpxj"),
    }


def demo_record(directory: Path) -> dict[str, Any]:
    from synaps_programplan.conflicts import analyze
    from synaps_programplan.io import load_plan, load_program
    from synaps_programplan.publish import attestation_error
    from synaps_programplan.scenarios import program_for_plan

    program = load_program(directory / "program.json")
    plans: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("plan_*.json")):
        result = load_plan(path)
        error = attestation_error(program_for_plan(program, result), result)
        if error:
            raise SystemExit(f"{path.name}: {error}")
        kpi = result.kpi
        finish = kpi.program_finish.isoformat() if kpi is not None and kpi.program_finish else None
        plans[result.scenario_id] = {
            "claim": result.outcome.claim.value,
            "finish": finish,
            "tardiness_wd": None if kpi is None else kpi.tardiness_wd,
            "moved_tasks": None if kpi is None else kpi.moved_count,
            "plan_hash": result.evidence.get("plan_hash"),
        }
    risk = json.loads((directory / "risk_A.json").read_text(encoding="utf-8"))
    gains = [int(item["p80_gain_wd"]) for item in risk.get("drivers") or []]
    witness = json.loads((directory / "witness_infeasible.json").read_text(encoding="utf-8"))
    dates: list[str] = []
    for item in witness.get("witness") or []:
        for day, month, year in re.findall(r"(\d{2})\.(\d{2})\.(\d{4})", str(item.get("text") or "")):
            iso = f"{year}-{month}-{day}"
            if iso not in dates:
                dates.append(iso)
    counts = analyze(program).summary()
    return {
        "command": ("SynAPS-ProgramPlan demo --projects 4 --time-limit 8 --risk-runs 40 --out-dir out/demo"),
        "plans": plans,
        "risk_p80": risk["program_finish"]["p80"],
        "deadlines_met_share": risk.get("deadlines_met_share"),
        "top_driver_p80_gain_wd": max(gains) if gains else None,
        "infeasible_dates": dates,
        "conflicts": dict(sorted(counts.items())),
    }


def _need(text: str, needle: str, where: str, gaps: list[str]) -> None:
    if needle not in text:
        gaps.append(f"{where}: missing {needle!r}")


def document_gaps(evidence: dict[str, Any]) -> list[str]:
    passed = int(evidence["tests"]["passed"])
    gaps: list[str] = []
    for relative, patterns in COUNT_PATTERNS.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for gap in pattern_gaps(text, patterns, passed):
            gaps.append(f"{relative}: {gap}")
    demo = evidence["demo"]
    plans = demo["plans"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    script = (ROOT / "docs" / "acceptance" / "demo-script.md").read_text(encoding="utf-8")
    english = (ROOT / "README.en.md").read_text(encoding="utf-8")
    row_a = plans["A"]
    row_b = plans["B"]
    row_e1 = plans["E1"]
    row_e2 = plans["E2"]
    row_p = plans["P"]
    deadline = demo["infeasible_dates"][0]
    _need(readme, ru_date(row_a["finish"]), "README.md", gaps)
    _need(readme, f"просрочка вех {row_a['tardiness_wd']} рабочих дней", "README.md", gaps)
    _need(readme, f"сдвинута {row_a['moved_tasks']} работа", "README.md", gaps)
    _need(readme, ru_date(demo["risk_p80"]), "README.md", gaps)
    _need(readme, ru_date(deadline), "README.md", gaps)
    _need(
        script,
        f"окончание {ru_date(row_b['finish'])} и просрочка {row_b['tardiness_wd']} рабочих дней",
        "demo-script.md",
        gaps,
    )
    _need(
        script,
        f"{ru_date(row_a['finish'])}, просрочка вех {row_a['tardiness_wd']} р.д., "
        f"сдвинута {row_a['moved_tasks']} работа",
        "demo-script.md",
        gaps,
    )
    _need(
        script,
        f"Окончание {ru_date(row_e1['finish'])}, просрочка {row_e1['tardiness_wd']} р.д.",
        "demo-script.md",
        gaps,
    )
    if row_e2["tardiness_wd"] == 0 and row_e2["claim"] == "OPTIMAL":
        _need(
            script,
            f"Окончание {ru_date(row_e2['finish'])}, просрочки нет, вердикт OPTIMAL",
            "demo-script.md",
            gaps,
        )
    if row_p["claim"] == "OPTIMAL":
        _need(script, "Вердикт `OPTIMAL` у «Приоритета»", "demo-script.md", gaps)
    _need(script, ru_date(demo["risk_p80"]), "demo-script.md", gaps)
    _need(script, f"{demo['top_driver_p80_gain_wd']} р.д.", "demo-script.md", gaps)
    _need(script, ru_date(deadline), "demo-script.md", gaps)
    counts = demo["conflicts"]
    conflict_line = (
        f"{counts['SHARED_CONTENTION']} споров за общий ресурс, "
        f"{counts['SKILL_POOL_OVERLOAD']} перегрузок пулов специалистов, "
        f"{counts['LINK_BROKEN']} разорванные связи, "
        f"{counts['DEADLINE_AT_RISK']} срока под угрозой"
    )
    _need(script, conflict_line, "demo-script.md", gaps)
    _need(english, en_date(row_a["finish"]), "README.en.md", gaps)
    _need(english, f"{row_a['tardiness_wd']} working days", "README.en.md", gaps)
    _need(english, f"{row_a['moved_tasks']} tasks moved", "README.en.md", gaps)
    _need(english, en_date(demo["risk_p80"]), "README.en.md", gaps)
    _need(english, en_date(deadline), "README.en.md", gaps)
    _need(english, f"`{row_a['claim']}`", "README.en.md", gaps)
    return gaps


def policy_gaps() -> list[str]:
    gaps: list[str] = []
    hook = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    if "unexpected skip" not in hook or "session.exitstatus" not in hook:
        gaps.append("tests/conftest.py does not fail the run when a test is skipped")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    if "build_evidence.py --check" not in workflow:
        gaps.append("CI does not check evidence.json")
    pins = re.findall(r"uses: actions/\S+@(\S+)", workflow)
    if not pins:
        gaps.append("CI workflow has no actions to pin")
    for pin in pins:
        if re.fullmatch(r"[0-9a-f]{40}", pin) is None:
            gaps.append(f"action pin {pin} is not a full commit SHA")
    return gaps


def _diff(path: str, left: Any, right: Any, out: list[str]) -> None:
    if left == right:
        return
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            _diff(f"{path}.{key}", left.get(key), right.get(key), out)
        return
    out.append(f"{path}: evidence {left!r} != measured {right!r}")


def check(demo_dir: Path | None) -> list[str]:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    gaps = document_gaps(evidence) + policy_gaps()
    tests = evidence["tests"]
    if tests["skipped"] != 0 or tests["failed"] != 0:
        gaps.append(f"evidence.json records skips or failures: {tests}")
    collected = collected_tests()
    if collected != tests["passed"]:
        gaps.append(f"collected {collected} tests, evidence.json says {tests['passed']}")
    if demo_dir is not None:
        _diff("demo", evidence["demo"], demo_record(demo_dir), gaps)
    return gaps


def build(demo_dir: Path) -> dict[str, Any]:
    passed = collected_tests()
    return {
        "tests": {"passed": passed, "skipped": 0, "failed": 0},
        "demo": demo_record(demo_dir),
        "host": environment(),
        "note": (
            "CI compares the documents with this file and does not re-solve the demonstration. "
            "An 8 second limit can change a FEASIBLE plan on a machine of different speed."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build or check evidence.json")
    parser.add_argument("--demo-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        gaps = check(args.demo_dir)
        if gaps:
            sys.stderr.write("evidence check failed:\n" + "\n".join(gaps) + "\n")
            return 1
        return 0
    if args.demo_dir is None:
        sys.stderr.write("build_evidence: pass --demo-dir, or --check to verify the committed file\n")
        return 2
    payload = build(args.demo_dir)
    EVIDENCE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
