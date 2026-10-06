"""No exit path may publish dates of a plan that was not accepted for this program."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from synaps_programplan.api import app
from synaps_programplan.cli import main
from synaps_programplan.io import load_program, save_plan
from synaps_programplan.publish import attestation_error
from synaps_programplan.report import report_data
from synaps_programplan.result import PlanResult
from tests.conftest import dep, program, task
from tests.test_cli import _solve

client = TestClient(app)


def _solved() -> tuple[dict, dict]:
    prog = program([task("a", 2), task("b", 2)], [dep("a", "b")])
    response = client.post("/solve", json={"program": prog.model_dump(mode="json"), "solver": "greedy"})
    assert response.status_code == 200
    return prog.model_dump(mode="json"), response.json()["result"]


def test_check_rejects_an_unaccepted_plan_even_when_the_dates_are_clean() -> None:
    body, plan = _solved()
    plan["outcome"]["ok"] = False
    plan["outcome"]["claim"] = "REJECTED"
    response = client.post("/check", json={"program": body, "plan": plan})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["accepted"] is False
    assert detail["ok"] is False
    assert "start" not in detail


def test_check_and_risk_reject_a_rewritten_program_hash() -> None:
    body, plan = _solved()
    plan["evidence"]["input_hash"] = "0" * 64
    checked = client.post("/check", json={"program": body, "plan": plan})
    assert checked.status_code == 409
    risk = client.post("/risk", json={"program": body, "plan": plan, "runs": 2})
    assert risk.status_code == 409


def test_report_export_and_cli_check_do_not_publish_unaccepted_dates(tmp_path: Path) -> None:
    program_path, plan_path, code = _solve(tmp_path, None)
    assert code == 0
    loaded = load_program(program_path)
    stored = PlanResult.model_validate_json(plan_path.read_text(encoding="utf-8"))
    finish = stored.task("b").finish.isoformat()
    rejected = stored.model_copy(update={"outcome": stored.outcome.model_copy(update={"ok": False})})
    assert attestation_error(loaded, rejected)
    save_plan(rejected, plan_path)
    assert main(["check", str(program_path), str(plan_path)]) == 1
    exported = tmp_path / "out.xml"
    assert main(["export", str(program_path), str(plan_path), "--out", str(exported)]) == 1
    assert not exported.exists()
    payload = report_data(loaded, [rejected], analysis=None)
    assert payload["scenarios"][0]["ok"] is False
    assert "tasks" not in payload["scenarios"][0]
    assert finish not in str(payload["comparison"])
