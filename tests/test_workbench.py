from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synaps_programplan.auth import Role, TokenStore
from synaps_programplan.edits import apply_moves, check_moves, repair_with_moves
from synaps_programplan.journal import append_decision, read_journal, verify_journal
from synaps_programplan.model import OKRProgram, TaskStatus
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.result import PlanResult
from synaps_programplan.workbench import Workbench, create_app
from tests.conftest import dep, program, stand, task, uses


def _case() -> tuple[OKRProgram, PlanResult]:
    prog = program(
        [
            task("a", 3, demands=uses("st")),
            task("b", 2, demands=uses("st")),
            task("c", 2),
            task("m", 0, deadline=date(2026, 10, 30)),
        ],
        [dep("a", "b"), dep("b", "m"), dep("c", "m")],
        resources=[stand()],
    )
    accepted = plan(prog, SolveConfig(time_limit_s=5))
    assert accepted.outcome.ok
    return prog, accepted


# ---------- edits ----------


def test_moves_snap_to_working_days_and_keep_others() -> None:
    prog, accepted = _case()
    rows = {r.task_id: r for r in apply_moves(prog, accepted, {"c": date(2026, 10, 17)})}  # Saturday
    assert rows["c"].start == date(2026, 10, 19)
    assert rows["c"].finish == date(2026, 10, 20)
    assert rows["a"].start == accepted.task("a").start


def test_moving_before_a_predecessor_is_a_hard_violation() -> None:
    prog, accepted = _case()
    checked = check_moves(prog, accepted, {"b": date(2026, 10, 5)})
    assert not checked["ok"]
    codes = {v["code"] for v in checked["violations"] if v["severity"] == "hard"}
    assert {"PRECEDENCE_FS_BROKEN", "CAPACITY_EXCEEDED"} <= codes


def test_moving_a_milestone_past_its_deadline_is_flagged() -> None:
    prog, accepted = _case()
    checked = check_moves(prog, accepted, {"m": date(2026, 11, 6)})
    assert checked["hard"] >= 1
    assert checked["kpi"]["late_count"] >= 1


def test_harmless_move_is_clean() -> None:
    prog, accepted = _case()
    checked = check_moves(prog, accepted, {"c": date(2026, 10, 6)})
    assert checked["ok"], checked["violations"]
    assert checked["moved"][0]["start"] == "2026-10-06"
    late = check_moves(prog, accepted, {"c": date(2026, 10, 12)})
    assert {v["code"] for v in late["violations"]} >= {"PRECEDENCE_FS_BROKEN"}


def test_unknown_task_or_rejected_base_is_refused() -> None:
    prog, accepted = _case()
    with pytest.raises(ValueError, match="unknown tasks"):
        apply_moves(prog, accepted, {"zz": date(2026, 10, 12)})
    rejected = accepted.model_copy(update={"outcome": accepted.outcome.model_copy(update={"ok": False})})
    with pytest.raises(ValueError, match="outcome.ok"):
        apply_moves(prog, rejected, {"c": date(2026, 10, 12)})


def test_repair_keeps_the_pinned_edit_and_moves_the_rest() -> None:
    prog, accepted = _case()
    repaired = repair_with_moves(prog, accepted, {"a": date(2026, 10, 12)}, SolveConfig(time_limit_s=5))
    assert repaired.outcome.ok
    assert repaired.task("a").start == date(2026, 10, 12)
    assert repaired.task("b").start >= date(2026, 10, 15)


def test_stable_repair_disturbs_only_what_the_edit_forces() -> None:
    prog, accepted = _case()
    repaired = repair_with_moves(prog, accepted, {"a": date(2026, 10, 12)}, SolveConfig(time_limit_s=5))
    assert repaired.outcome.ok and repaired.metadata["replan"]["mode"] == "stable"
    for row in repaired.tasks:
        if row.task_id != "a":
            assert row.start_index >= accepted.task(row.task_id).start_index
    assert repaired.task("c").start == accepted.task("c").start


def test_stable_repair_falls_back_to_optimise_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    import synaps_programplan.edits as edits

    prog, accepted = _case()
    real = edits.plan

    def no_stable_plan(program: OKRProgram, config: SolveConfig, **kwargs: object) -> PlanResult:
        result = real(program, config, **kwargs)  # type: ignore[arg-type]
        if config.objective == "stability":
            result.outcome = result.outcome.model_copy(update={"ok": False})
        return result

    monkeypatch.setattr(edits, "plan", no_stable_plan)
    repaired = repair_with_moves(prog, accepted, {"a": date(2026, 10, 12)}, SolveConfig(time_limit_s=5))
    assert repaired.outcome.ok
    assert repaired.metadata["replan"]["mode"] == "optimise"
    assert "stable_claim" in repaired.metadata["replan"]
    with pytest.raises(ValueError, match="mode"):
        repair_with_moves(prog, accepted, {"a": date(2026, 10, 12)}, mode="fast")  # type: ignore[arg-type]


def test_repair_refuses_started_work() -> None:
    prog = program(
        [task("a", 5, status=TaskStatus.IN_PROGRESS, actual_start=date(2026, 10, 1), remaining_wd=3)]
    )
    accepted = plan(prog, SolveConfig(time_limit_s=5))
    with pytest.raises(ValueError, match="started or finished"):
        repair_with_moves(prog, accepted, {"a": date(2026, 10, 12)})


# ---------- journal and roles ----------


def test_journal_chain_detects_edits(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    for action in ("accept", "reject"):
        append_decision(
            path,
            action=action,
            user="u",
            role="manager",
            scenario_id="A",
            plan_hash="h",
            input_hash="i",
            reason="r",
        )
    assert verify_journal(path).ok
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["reason"] = "changed"
    path.write_text("\n".join([json.dumps(record), lines[1]]) + "\n", encoding="utf-8")
    check = verify_journal(path)
    assert not check.ok and check.broken_at == 1
    path.write_text(lines[1] + "\n", encoding="utf-8")
    assert verify_journal(path).broken_at == 1


def test_journal_witness_catches_a_cut_tail_and_a_replaced_file(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    for action in ("accept", "reject"):
        append_decision(
            path,
            action=action,
            user="u",
            role="manager",
            scenario_id="A",
            plan_hash="h",
            input_hash="i",
            reason="r",
        )
    head = read_journal(path)[-1]["hash"]
    assert verify_journal(path, anchor=head).ok
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n", encoding="utf-8")
    cut = verify_journal(path)
    assert not cut.ok and "witness" in cut.reason
    other = tmp_path / "other.jsonl"
    append_decision(
        other,
        action="accept",
        user="u",
        role="manager",
        scenario_id="A",
        plan_hash="h",
        input_hash="i",
        reason="fresh",
    )
    path.write_text(other.read_text(encoding="utf-8"), encoding="utf-8")
    replaced = verify_journal(path)
    assert not replaced.ok and "witness" in replaced.reason
    assert not verify_journal(other, anchor=head).ok


def test_seal_does_not_hide_a_truncated_journal(tmp_path: Path) -> None:
    from synaps_programplan.journal import seal_journal

    path = tmp_path / "j.jsonl"
    append_decision(
        path,
        action="accept",
        user="u",
        role="manager",
        scenario_id="A",
        plan_hash="h",
        input_hash="i",
        reason="r",
    )
    path.write_text("", encoding="utf-8")
    assert not seal_journal(path).ok


def test_stable_churn_is_only_what_the_edit_reaches() -> None:
    prog, accepted = _case()
    repaired = repair_with_moves(prog, accepted, {"a": date(2026, 10, 12)}, SolveConfig(time_limit_s=5))
    churn = repaired.metadata["replan"]["churn"]
    assert churn["other"] == 0
    assert churn["downstream"] >= 1
    assert churn["moved"] == churn["pinned"] + churn["downstream"] + churn["resource"] + churn["other"]


def test_proxy_identity_is_rejected_without_the_shared_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_PROXY_SECRET", "s3cret")
    prog, accepted = _case()
    bench = Workbench(program=prog, plans=[accepted], journal=tmp_path / "j.jsonl")
    client = TestClient(create_app(bench))
    ignored = client.get("/api/data", headers={"X-Remote-User": "ivanov", "X-Remote-Role": "observer"})
    assert ignored.status_code == 200 and ignored.json()["workbench"]["user"] == "local"
    rejected = client.get(
        "/api/data",
        headers={"X-Synaps-Proxy-Secret": "nope", "X-Remote-User": "ivanov", "X-Remote-Role": "planner"},
    )
    assert rejected.status_code == 401
    proxied = client.get(
        "/api/data",
        headers={"X-Synaps-Proxy-Secret": "s3cret", "X-Remote-User": "ivanov", "X-Remote-Role": "observer"},
    )
    assert proxied.status_code == 200
    assert proxied.json()["workbench"]["user"] == "ivanov"
    assert proxied.json()["workbench"]["role"] == "observer"
    repair = client.post(
        "/api/repair",
        json={"scenario": accepted.scenario_id, "moves": {"a": "2026-10-12"}},
        headers={"X-Synaps-Proxy-Secret": "s3cret", "X-Remote-User": "ivanov", "X-Remote-Role": "observer"},
    )
    assert repair.status_code == 403


def test_workbench_refuses_a_journal_whose_witness_disagrees(tmp_path: Path) -> None:
    prog, accepted = _case()
    path = tmp_path / "j.jsonl"
    append_decision(
        path,
        action="accept",
        user="u",
        role="manager",
        scenario_id="A",
        plan_hash="h",
        input_hash="i",
        reason="r",
    )
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="witness"):
        Workbench(program=prog, plans=[accepted], journal=path)


def test_a_wrong_length_proxy_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_PROXY_SECRET", "s3cret")
    from synaps_programplan.auth import proxy_principal

    assert proxy_principal("nope", "ivanov", "planner") is None
    assert proxy_principal("s3cret-but-longer", "ivanov", "planner") is None


def test_non_loopback_bind_requires_roles_and_tls() -> None:
    from synaps_programplan.workbench import exposure_refusal

    assert exposure_refusal("127.0.0.1", authenticated=False, tls=False) is None
    assert exposure_refusal("0.0.0.0", authenticated=False, tls=True) is not None
    assert exposure_refusal("0.0.0.0", authenticated=True, tls=False) is not None
    assert exposure_refusal("0.0.0.0", authenticated=True, tls=True) is None


def test_tokens_map_to_roles() -> None:
    import hashlib

    digest = hashlib.sha256(b"s3cret").hexdigest()
    store = TokenStore.parse(f"t1=planner:ivanov, sha256:{digest}=observer:petrov")
    assert store.authenticate("t1").role is Role.PLANNER  # type: ignore[union-attr]
    observer = store.authenticate("s3cret")
    assert observer is not None and observer.user == "petrov" and not observer.may("decide")
    assert store.authenticate("nope") is None
    assert store.authenticate(None) is None
    with pytest.raises(ValueError):
        TokenStore.parse("broken")
    assert TokenStore.parse("").authenticate(None).role is Role.PLANNER  # type: ignore[union-attr]


# ---------- HTTP workbench ----------


def _client(tmp_path: Path, spec: str = "") -> tuple[TestClient, Workbench]:
    prog, accepted = _case()
    bench = Workbench(
        program=prog,
        plans=[accepted],
        journal=tmp_path / "decisions.jsonl",
        save_dir=tmp_path,
        config=SolveConfig(time_limit_s=5),
        tokens=TokenStore.parse(spec),
    )
    return TestClient(create_app(bench)), bench


def test_local_workbench_serves_the_report_and_checks(tmp_path: Path) -> None:
    client, bench = _client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert "editMode" in page.text and "default-src 'none'" in page.headers["content-security-policy"]
    data = client.get("/api/data").json()
    assert data["workbench"]["role"] == "planner" and data["scenarios"][0]["ok"]
    sid = accepted_id = bench.plans[0].scenario_id
    bad = client.post("/api/check", json={"scenario": sid, "moves": {"m": "2026-11-06"}}).json()
    assert bad["hard"] >= 1
    good = client.post("/api/check", json={"scenario": sid, "moves": {"c": "2026-10-06"}}).json()
    assert good["ok"]
    assert client.post("/api/check", json={"scenario": "nope", "moves": {}}).status_code == 404
    assert client.get(f"/api/plans/{accepted_id}/mspdi").text.startswith("<?xml")


def test_local_workbench_rejects_foreign_host_headers(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get("/api/data", headers={"host": "evil.example"}).status_code == 400


def test_repair_adds_an_accepted_variant_and_journals_it(tmp_path: Path) -> None:
    client, bench = _client(tmp_path)
    sid = bench.plans[0].scenario_id
    response = client.post("/api/repair", json={"scenario": sid, "moves": {"a": "2026-10-12"}})
    assert response.status_code == 200, response.text
    assert response.json()["scenario"] == "R1"
    assert [p.scenario_id for p in bench.plans] == [sid, "R1"]
    assert (tmp_path / "plan_R1.json").exists()
    records = read_journal(tmp_path / "decisions.jsonl")
    assert records[-1]["action"] == "repair" and records[-1]["details"]["moves"] == {"a": "2026-10-12"}


def test_restart_restores_journaled_variants_and_keeps_numbering(tmp_path: Path) -> None:
    client, bench = _client(tmp_path)
    sid = bench.plans[0].scenario_id
    assert client.post("/api/repair", json={"scenario": sid, "moves": {"a": "2026-10-12"}}).status_code == 200
    _, again = _client(tmp_path)
    assert again.restored == ["R1"] and [p.scenario_id for p in again.plans] == [sid, "R1"]
    second = TestClient(create_app(again)).post(
        "/api/repair", json={"scenario": sid, "moves": {"a": "2026-10-13"}}
    )
    assert second.json()["scenario"] == "R2"

    saved = tmp_path / "plan_R1.json"
    saved.write_text(saved.read_text(encoding="utf-8").replace("2026-10-12", "2026-10-09"), encoding="utf-8")
    _, tampered = _client(tmp_path)
    assert tampered.restored == ["R2"]


def test_infeasible_repair_is_not_published(tmp_path: Path) -> None:
    client, bench = _client(tmp_path)
    sid = bench.plans[0].scenario_id
    response = client.post("/api/repair", json={"scenario": sid, "moves": {"m": "2026-11-06"}})
    assert response.status_code == 409
    assert len(bench.plans) == 1


def test_roles_gate_decisions(tmp_path: Path) -> None:
    client, bench = _client(tmp_path, "pt=planner:ivanov,mt=manager:sidorov,ot=observer:petrov")
    sid = bench.plans[0].scenario_id
    body = {"scenario": sid, "action": "accept", "reason": "сроки ОКР-1 соблюдены"}
    assert client.get("/api/data").status_code == 401
    assert client.get("/api/data", headers={"Authorization": "Bearer wrong"}).status_code == 401
    observer = {"Authorization": "Bearer ot"}
    assert client.post("/api/check", json={"scenario": sid, "moves": {}}, headers=observer).status_code == 200
    assert client.post("/api/decisions", json=body, headers=observer).status_code == 403
    repair = {"scenario": sid, "moves": {"c": "2026-10-12"}}
    assert client.post("/api/repair", json=repair, headers={"Authorization": "Bearer mt"}).status_code == 403
    made = client.post("/api/decisions", json=body, headers={"Authorization": "Bearer mt"})
    assert made.status_code == 200
    assert made.json()["user"] == "sidorov" and made.json()["role"] == "manager"
    listing = client.get("/api/decisions", headers=observer).json()
    assert (
        listing["integrity"]["ok"]
        and listing["records"][0]["plan_hash"] == bench.plans[0].evidence["plan_hash"]
    )
    short = {"scenario": sid, "action": "reject", "reason": ""}
    assert (
        client.post("/api/decisions", json=short, headers={"Authorization": "Bearer mt"}).status_code == 422
    )
