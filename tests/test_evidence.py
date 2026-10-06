from __future__ import annotations

import json

from synaps_programplan.evidence import evidence_stamp


def test_evidence_config_with_a_set_is_json() -> None:
    stamp = evidence_stamp(
        input_hash="abc",
        config={"adjustments": {"ignore_due_projects": frozenset({"p2", "p1"})}},
        data_provenance="synthetic",
    )
    json.dumps(stamp)
    assert stamp["config"]["adjustments"]["ignore_due_projects"] == ["p1", "p2"]
