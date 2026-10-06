"""Write requirements.lock and sbom.cdx.json from the environment that runs this script.

Run on the same operating system and CPU as the closed-network machines, then
copy the two files with the wheelhouse. Versions are whatever is installed
here; the script does not download anything.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
from pathlib import Path

from synaps_programplan.versions import SYNAPS_COMMIT, VERSION

ROOT = Path(__file__).resolve().parents[1]

# name in the lock, distribution name, required for a runtime install
RUNTIME = (
    ("synaps", "synaps", True),
    ("pydantic", "pydantic", True),
    ("ortools", "ortools", True),
    ("defusedxml", "defusedxml", True),
    ("openpyxl", "openpyxl", True),
    ("fastapi", "fastapi", False),
    ("starlette", "starlette", False),
    ("httpx", "httpx", False),
    ("uvicorn", "uvicorn", False),
    ("mpxj", "mpxj", False),
    ("JPype1", "jpype1", False),
)


def main() -> int:
    components = []
    lines = [
        "# Runtime lock for SynAPS-ProgramPlan. Regenerate on the target platform:",
        "#   python scripts/write_delivery.py",
        f"# Product version {VERSION}. SynAPS commit {SYNAPS_COMMIT}.",
        "synaps @ git+https://github.com/KonkovDV/SynAPS.git@" + SYNAPS_COMMIT,
    ]
    for lock_name, dist_name, required in RUNTIME:
        if lock_name == "synaps":
            version = _version(dist_name)
            components.append(_component("synaps", version, github=True))
            continue
        try:
            version = _version(dist_name)
        except metadata.PackageNotFoundError:
            if required:
                raise
            lines.append(f"# {lock_name} not installed in this environment (optional extra)")
            continue
        lines.append(f"{lock_name}=={version}")
        components.append(_component(lock_name, version, github=False))
    (ROOT / "requirements.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "SynAPS-ProgramPlan",
                "version": VERSION,
            }
        },
        "components": components,
    }
    (ROOT / "sbom.cdx.json").write_text(
        json.dumps(bom, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return 0


def _version(dist_name: str) -> str:
    return metadata.version(dist_name)


def _component(name: str, version: str, *, github: bool) -> dict[str, str]:
    purl = f"pkg:github/KonkovDV/SynAPS@{SYNAPS_COMMIT}" if github else f"pkg:pypi/{name.lower()}@{version}"
    return {"type": "library", "name": name, "version": version, "purl": purl}


if __name__ == "__main__":
    raise SystemExit(main())
