from __future__ import annotations

from pathlib import Path

from synaps_programplan.journal import append_decision, verify_journal


def _record(path: Path, key: str | None = None) -> None:
    append_decision(
        path,
        action="accept",
        user="ivanov",
        role="planner",
        scenario_id="A",
        plan_hash="h",
        input_hash="i",
        reason="берём",
        key=key,
    )


def test_a_wrong_signature_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SYNAPS_PROGRAMPLAN_JOURNAL_KEY", raising=False)
    path = tmp_path / "decisions.jsonl"
    _record(path, "secret")
    assert verify_journal(path, key="secret").ok
    text = path.read_text(encoding="utf-8").replace('"sig": "', '"sig": "00')
    path.write_text(text, encoding="utf-8")
    check = verify_journal(path, key="secret")
    assert not check.ok
    assert check.broken_at == 1
    assert "signature" in check.reason


def test_unsigned_journal_still_verifies_without_a_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SYNAPS_PROGRAMPLAN_JOURNAL_KEY", raising=False)
    path = tmp_path / "decisions.jsonl"
    _record(path)
    assert "sig" not in path.read_text(encoding="utf-8")
    assert verify_journal(path).ok
    assert not verify_journal(path, key="secret").ok


def test_the_environment_key_signs_new_records(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_JOURNAL_KEY", "secret")
    path = tmp_path / "decisions.jsonl"
    _record(path)
    assert verify_journal(path).ok
    monkeypatch.setenv("SYNAPS_PROGRAMPLAN_JOURNAL_KEY", "other")
    assert not verify_journal(path).ok
