"""Self-check of a demonstration or pilot stand.

Answers one question before anyone opens the report: is this the pinned
build, is the language model off, and is every plan in the demo directory
publishable against the program it was solved on. Nothing here solves or
reaches the network.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from synaps_programplan.versions import SYNAPS_COMMIT

_REQUIRED = ("synaps", "synaps.precedence", "ortools", "pydantic", "defusedxml", "openpyxl")
_WORKBENCH = ("fastapi", "uvicorn", "httpx")
_MODEL_VARS = ("SYNAPS_PROGRAMPLAN_YANDEX_API_KEY", "SYNAPS_PROGRAMPLAN_YANDEX_FOLDER")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "blocking": self.blocking, "detail": self.detail}


def environment_checks() -> list[Check]:
    out = [
        Check(
            "python",
            sys.version_info >= (3, 12),
            f"{platform.python_version()} ({platform.system()} {platform.machine()})",
        )
    ]
    for module in _REQUIRED:
        out.append(_import_check(module, blocking=True))
    for module in _WORKBENCH:
        out.append(_import_check(module, blocking=False))
    try:
        ortools = importlib.metadata.version("ortools")
    except importlib.metadata.PackageNotFoundError:
        ortools = "absent"
    out.append(Check("ortools_version", ortools == "9.15.6755", ortools))
    out.append(Check("synaps_commit", len(SYNAPS_COMMIT) == 40, SYNAPS_COMMIT))
    present = [name for name in _MODEL_VARS if os.environ.get(name, "").strip()]
    out.append(
        Check(
            "language_model_off",
            not present,
            "переменные Yandex не заданы" if not present else "заданы: " + ", ".join(present),
        )
    )
    return out


def demo_checks(directory: Path) -> list[Check]:
    from synaps_programplan.explanations import explanation_gaps
    from synaps_programplan.io import load_plan, load_program
    from synaps_programplan.publish import attestation_error
    from synaps_programplan.scenarios import program_for_plan

    out: list[Check] = []
    program_path = directory / "program.json"
    if not program_path.is_file():
        return [Check("demo_program", False, f"нет {program_path}")]
    program = load_program(program_path)
    plans = sorted(directory.glob("plan_*.json"))
    out.append(Check("demo_plans", bool(plans), f"{len(plans)} файлов плана"))
    base_hash = ""
    for path in plans:
        result = load_plan(path)
        solved = program_for_plan(program, result)
        error = attestation_error(solved, result)
        gaps = explanation_gaps(solved, result) if error is None else []
        detail = error or (f"{len(gaps)} причин расходятся с планом" if gaps else result.outcome.claim.value)
        out.append(Check(f"plan:{result.scenario_id}", error is None and not gaps, detail))
        if result.scenario_id == "A" and error is None:
            base_hash = str(result.evidence.get("plan_hash") or "")
    for name in ("report.html", "report_infeasible.html", "plan.xml", "risk_A.json"):
        path = directory / name
        out.append(Check(f"file:{name}", path.is_file() and path.stat().st_size > 0, str(path)))
    report = directory / "report.html"
    if base_hash and report.is_file():
        out.append(
            Check(
                "report_matches_plan_A",
                base_hash in report.read_text(encoding="utf-8"),
                base_hash[:16],
            )
        )
    return out


def _import_check(module: str, *, blocking: bool) -> Check:
    try:
        importlib.import_module(module)
    except ImportError as exc:
        return Check(f"import:{module}", False, str(exc), blocking)
    return Check(f"import:{module}", True, "ok", blocking)


def stand_ok(checks: list[Check]) -> bool:
    return all(item.ok for item in checks if item.blocking)
