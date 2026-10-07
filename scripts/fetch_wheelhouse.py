"""Download wheels and write a hashed requirement file for an offline install.

The wheel directory is not committed. Copy it into the closed network and
install with::

    pip install --no-index --find-links wheelhouse -r requirements-hashed.txt
    pip install --no-index --no-deps wheelhouse/synaps_programplan-<version>-py3-none-any.whl

The product wheel changes with every commit, so it stays out of the hashed file.
SynAPS is built from the pinned checkout when ``SYNAPS_SOURCE`` points at it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
HASHED = ROOT / "requirements-hashed.txt"
WHEELHOUSE = ROOT / "wheelhouse"


def main() -> int:
    WHEELHOUSE.mkdir(exist_ok=True)
    packages = [
        line.strip()
        for line in LOCK.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("synaps ")
    ]
    subprocess.check_call(
        [sys.executable, "-m", "pip", "download", "-d", str(WHEELHOUSE), *packages],
    )
    source = os.environ.get("SYNAPS_SOURCE", r"C:\SynAPS")
    pin = "1feb50b33568cb86c1bfd12b9d7cd7247e17ee49"
    if Path(source).joinpath("pyproject.toml").is_file():
        revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
        if revision == pin:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "wheel", source, "-w", str(WHEELHOUSE), "--no-deps"],
            )
    subprocess.check_call(
        [sys.executable, "-m", "pip", "wheel", str(ROOT), "-w", str(WHEELHOUSE), "--no-deps"],
    )
    grouped: dict[str, list[str]] = {}
    for path in sorted(WHEELHOUSE.glob("*")):
        if path.suffix not in {".whl", ".tar.gz", ".zip"} and not path.name.endswith(".tar.gz"):
            continue
        if path.name.startswith("synaps_programplan-"):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        grouped.setdefault(_requirement_name(path.name), []).append(digest)
    lines = [
        "# Hashed wheels for an offline install. Produced by scripts/fetch_wheelhouse.py.",
        "# SynAPS commit 1feb50b33568cb86c1bfd12b9d7cd7247e17ee49.",
    ]
    for name, digests in grouped.items():
        body = " \\\n    ".join(f"--hash=sha256:{digest}" for digest in digests)
        lines.append(f"{name} \\\n    {body}")
    HASHED.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def _requirement_name(filename: str) -> str:
    stem = filename
    for suffix in (".tar.gz", ".whl", ".zip"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # wheel: name-version-pyver-abi-plat
    parts = stem.split("-")
    if filename.endswith(".whl") and len(parts) >= 2:
        return f"{parts[0]}=={parts[1]}"
    if len(parts) >= 2:
        return f"{parts[0]}=={parts[1]}"
    return stem


if __name__ == "__main__":
    raise SystemExit(main())
