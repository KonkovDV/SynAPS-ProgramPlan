"""A what-if plan is checked against the program it was solved on, and is not silently re-planned."""

from __future__ import annotations

from fastapi.testclient import TestClient

from synaps_programplan.planner import SolveConfig
from synaps_programplan.scenarios import WhatIf, run_scenarios
from synaps_programplan.workbench import Workbench, create_app
from tests.conftest import dep, program, stand, task, uses


def _what_if_bench(tmp_path, what_if: WhatIf):
    prog = program(
        [task("x", 3, project="p1", demands=uses("st")), task("y", 2, project="p2", demands=uses("st"))],
        [dep("x", "y")],
        resources=[stand()],
    )
    scenarios = run_scenarios(prog, SolveConfig(solver="greedy"), what_ifs=[what_if])
    found = next(p for p in scenarios.plans if p.metadata.get("what_if") and p.outcome.ok)
    bench = Workbench(program=prog, plans=[found], journal=tmp_path / "decisions.jsonl")
    return TestClient(create_app(bench)), found


def test_a_move_on_a_what_if_plan_is_checked_against_its_own_program(tmp_path) -> None:
    # The what-if drops project p2, which the base program still links to x.
    client, found = _what_if_bench(tmp_path, WhatIf(label="без p2", drop_projects=["p2"]))
    response = client.post("/api/check", json={"scenario": found.scenario_id, "moves": {"x": "2026-10-19"}})
    assert response.status_code == 200
    assert "kpi" in response.json()


def test_repair_refuses_a_what_if_plan_instead_of_dropping_its_change(tmp_path) -> None:
    client, found = _what_if_bench(tmp_path, WhatIf(label="+1 стенд", add_capacity={"st": 1}))
    response = client.post(
        "/api/repair", json={"scenario": found.scenario_id, "moves": {"x": "2026-10-19"}, "mode": "stable"}
    )
    assert response.status_code == 422
    assert "что если" in response.json()["detail"]
