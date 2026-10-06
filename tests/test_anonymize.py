from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from scripts.anonymize_program import anonymize, main

from tests.conftest import program, task


def test_names_disappear_and_every_date_moves_by_the_same_number(tmp_path: Path) -> None:
    secret = "Секретное изделие"
    row = task("a", 5, planned_start=date(2026, 10, 5), planned_finish=date(2026, 10, 9))
    prog = program([row.model_copy(update={"name": secret})])
    source = tmp_path / "program.json"
    target = tmp_path / "out.json"
    source.write_text(prog.model_dump_json(), encoding="utf-8")
    assert main([str(source), "--shift-days", "100", "--out", str(target)]) == 0
    text = target.read_text(encoding="utf-8")
    assert secret not in text
    payload = json.loads(text)
    assert payload["tasks"][0]["duration_wd"] == 5
    assert payload["tasks"][0]["planned_start"] == "2027-01-13"
    again = anonymize(json.loads(source.read_text(encoding="utf-8")), 100)
    assert again["tasks"][0]["name"] == payload["tasks"][0]["name"]
    shifted = date.fromisoformat(payload["program"]["horizon_start"])
    original = date.fromisoformat(json.loads(source.read_text(encoding="utf-8"))["program"]["horizon_start"])
    assert shifted - original == timedelta(days=100)
