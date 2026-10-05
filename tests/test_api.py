from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from synaps_programplan.api import app
from tests.conftest import dep, program, task

client = TestClient(app)


def _body(prog) -> dict:
    return prog.model_dump(mode="json")


def test_version_names_the_product() -> None:
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json()["name"] == "SynAPS-ProgramPlan"


def test_solve_rejects_an_impossible_deadline() -> None:
    prog = program([task("a", 5, deadline=date(2026, 10, 6))])
    response = client.post("/solve", json={"program": _body(prog), "solver": "cpsat", "time_limit_s": 5})
    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["accepted"] is False
    assert body["result"]["outcome"]["ok"] is False


def test_solve_and_risk_on_a_chain() -> None:
    prog = program([task("a", 2), task("b", 2)], [dep("a", "b")])
    solved = client.post("/solve", json={"program": _body(prog), "solver": "greedy"})
    assert solved.status_code == 200
    assert solved.json()["accepted"] is True
    risk = client.post(
        "/risk",
        json={"program": _body(prog), "plan": solved.json()["result"], "runs": 5, "seed": 1},
    )
    assert risk.status_code == 200
    assert risk.json()["scheduled_runs"] == 5
    assert "не доказанная вероятность" in risk.json()["note"]
    checked = client.post("/check", json={"program": _body(prog), "plan": solved.json()["result"]})
    assert checked.json() == {"ok": True, "hard": 0, "violations": []}
    moved = client.post(
        "/check",
        json={"program": _body(prog), "plan": solved.json()["result"], "moves": {"b": "2026-10-05"}},
    )
    assert moved.status_code == 200 and moved.json()["hard"] >= 1
    unknown = client.post(
        "/check", json={"program": _body(prog), "plan": solved.json()["result"], "moves": {"z": "2026-10-05"}}
    )
    assert unknown.status_code == 422
