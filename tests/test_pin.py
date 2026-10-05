from __future__ import annotations

from pathlib import Path

from synaps_programplan.versions import SYNAPS_COMMIT


def test_synaps_pin_is_a_commit_and_matches_the_published_files() -> None:
    assert len(SYNAPS_COMMIT) == 40
    assert "PLACEHOLDER" not in SYNAPS_COMMIT
    for name in (
        "pyproject.toml",
        "README.md",
        "CLAIMS_REGISTRY.md",
        "src/synaps_programplan/versions.py",
    ):
        assert SYNAPS_COMMIT in Path(name).read_text(encoding="utf-8"), name
