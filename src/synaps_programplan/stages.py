"""Typical OKR stages. The list lives in ``okr_stages.json``, not in code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StageSpec:
    code: str
    name: str
    skills: tuple[str, ...]
    test: bool = False


def stage_catalog(path: Path | None = None) -> tuple[StageSpec, ...]:
    """Read the stage catalog. Codes must be unique."""
    source = path or Path(__file__).with_name("okr_stages.json")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{source.name} must be a non-empty list of stages")
    stages: list[StageSpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{source.name} has a stage that is not an object")
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        skills_raw = item.get("skills", [])
        if not code or not name or not isinstance(skills_raw, list) or not skills_raw:
            raise ValueError(f"{source.name} stage {code or '?'} needs a code, a name and skills")
        if code in seen:
            raise ValueError(f"{source.name} repeats stage {code}")
        seen.add(code)
        stages.append(
            StageSpec(
                code=code,
                name=name,
                skills=tuple(str(skill) for skill in skills_raw),
                test=bool(item.get("test", False)),
            )
        )
    return tuple(stages)
