from __future__ import annotations

from pathlib import Path

from synaps_okrplan.versions import SYNAPS_COMMIT


def test_synaps_pin_is_a_commit_and_matches_pyproject() -> None:
    assert len(SYNAPS_COMMIT) == 40
    assert SYNAPS_COMMIT in Path("pyproject.toml").read_text(encoding="utf-8")
    assert "PLACEHOLDER" not in SYNAPS_COMMIT
