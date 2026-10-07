"""Schema migration, canonical input hash, and a diff of two programme versions."""

from __future__ import annotations

import json
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from synaps_programplan.cli import main
from synaps_programplan.diffing import diff_programs
from synaps_programplan.evidence import fingerprint
from synaps_programplan.io import load_program
from synaps_programplan.model import PROGRAM_SCHEMA
from tests.conftest import dep, program, stand, task


def _reorder(value, shift: int):
    if isinstance(value, dict):
        keys = list(value)
        if keys:
            cut = shift % len(keys)
            keys = keys[cut:] + keys[:cut]
        return {key: _reorder(item, shift + 1) for key, item in ((key, value[key]) for key in keys)}
    if isinstance(value, list):
        return [_reorder(item, shift + 1) for item in value]
    return value


def test_a_file_without_schema_version_loads_as_the_current_one(tmp_path) -> None:
    original = program([task("a", 2), task("b", 1)], [dep("a", "b")])
    raw = json.loads(original.model_dump_json())
    raw.pop("schema_version", None)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_program(path)
    assert loaded.schema_version == PROGRAM_SCHEMA
    assert fingerprint(loaded) == fingerprint(original)


def test_an_unknown_schema_version_is_refused(tmp_path) -> None:
    original = program([task("a", 1)])
    raw = json.loads(original.model_dump_json())
    raw["schema_version"] = "SynAPS-ProgramPlan.program.v9"
    path = tmp_path / "future.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="unknown program schema_version"):
        load_program(path)
    assert main(["diff", str(path), str(path)]) == 2


def test_diff_names_changed_tasks_links_resources_and_dates(tmp_path, capsys) -> None:
    before = program(
        [task("a", 2, planned_start=date(2027, 1, 11)), task("b", 1)],
        [dep("a", "b")],
        resources=[stand("st")],
    )
    after = before.model_copy(deep=True)
    after.tasks[0].planned_start = date(2027, 2, 2)
    after.dependencies[0].lag_wd = 4
    after.resources[0].capacity_units = 2
    found = diff_programs(before, after)
    assert found["tasks"]["changed"][0]["id"] == "a"
    assert found["dependencies"]["changed"][0]["lag_wd"] == 4
    assert found["resources"]["changed"][0]["id"] == "st"
    assert found["dates"] == [
        {
            "entity": "task",
            "id": "a",
            "field": "planned_start",
            "before": "2027-01-11",
            "after": "2027-02-02",
        }
    ]
    left = tmp_path / "v1.json"
    right = tmp_path / "v2.json"
    left.write_text(before.model_dump_json(), encoding="utf-8")
    right.write_text(after.model_dump_json(), encoding="utf-8")
    assert main(["diff", str(left), str(right)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dates"][0]["field"] == "planned_start"


@given(st.integers(min_value=0, max_value=30))
@settings(max_examples=25, deadline=None)
def test_key_order_does_not_change_the_input_hash(shift: int) -> None:
    original = program(
        [task("a", 2, planned_start=date(2027, 1, 11)), task("b", 1)],
        [dep("a", "b", lag=2)],
        resources=[stand("st")],
    )
    shuffled = _reorder(json.loads(original.model_dump_json()), shift)
    loaded = load_program_from_mapping(shuffled)
    assert fingerprint(loaded) == fingerprint(original)


def load_program_from_mapping(payload: dict):
    from synaps_programplan.model import OKRProgram

    return OKRProgram.model_validate(payload)
