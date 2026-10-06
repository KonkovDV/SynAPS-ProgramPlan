from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from synaps_programplan import api
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
    assert moved.status_code == 409 and moved.json()["detail"]["hard"] >= 1
    assert moved.json()["detail"]["ok"] is False
    unknown = client.post(
        "/check", json={"program": _body(prog), "plan": solved.json()["result"], "moves": {"z": "2026-10-05"}}
    )
    assert unknown.status_code == 422


def test_remote_client_without_a_token_is_refused(monkeypatch) -> None:
    monkeypatch.delenv("SYNAPS_PROGRAMPLAN_TOKENS", raising=False)
    remote = TestClient(app, client=("203.0.113.8", 9))
    assert remote.get("/version").status_code == 401
    local = TestClient(app, client=("127.0.0.1", 9))
    assert local.get("/version").status_code == 200


def test_remote_client_with_a_token_is_answered(monkeypatch) -> None:
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_TOKENS", "secret=planner:ivanov")
    remote = TestClient(app, client=("203.0.113.8", 9))
    assert remote.get("/version").status_code == 401
    allowed = remote.get("/version", headers={"Authorization": "Bearer secret"})
    assert allowed.status_code == 200


def test_rate_limit_refuses_the_next_request(monkeypatch) -> None:
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_RATE_PER_MIN", "1")
    api._HITS.clear()
    local = TestClient(app, client=("127.0.0.1", 9))
    assert local.get("/version").status_code == 200
    assert local.get("/version").status_code == 429
    api._HITS.clear()


def test_the_workbench_does_not_import_the_compute_api() -> None:
    from pathlib import Path

    source = Path("src/synaps_programplan/workbench.py").read_text(encoding="utf-8")
    assert "synaps_programplan.api" not in source
    assert "import api" not in source


def test_oversized_body_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(api, "MAX_BODY_BYTES", 32)
    response = client.post("/version", content=b"x" * 64)
    assert response.status_code == 413
