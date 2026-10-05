"""Import / export: canonical JSON, MS Project XML (MSPDI)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from synaps_okrplan.model import OKRProgram
from synaps_okrplan.result import PlanResult


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_program(path: Path) -> OKRProgram:
    return OKRProgram.model_validate_json(path.read_text(encoding="utf-8"))


def save_program(program: OKRProgram, path: Path) -> None:
    path.write_text(program.model_dump_json(indent=1, exclude_defaults=True), encoding="utf-8")


def load_plan(path: Path) -> PlanResult:
    return PlanResult.model_validate_json(path.read_text(encoding="utf-8"))


def save_plan(plan: PlanResult, path: Path) -> None:
    path.write_text(plan.model_dump_json(indent=1), encoding="utf-8")
