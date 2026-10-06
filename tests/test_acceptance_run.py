from __future__ import annotations

from pathlib import Path

from scripts.acceptance_run import main


def test_draft_records_the_bench_and_keeps_the_laboratory_level(tmp_path: Path) -> None:
    out = tmp_path / "protocol.md"
    assert main(["--out", str(out), "--skip-pytest"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "ISO 16290 TRL 4" in text
    assert "Подписи заказчика и исполнителя" in text
    assert "не выполнялось" not in text
    assert "`psplib_j60.json`" in text
    assert "`psplib_j120.json`" in text
