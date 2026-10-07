"""Actual progress stays put, and the forecast of the remainder is not before the status date."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from synaps_programplan.checker import check_plan
from synaps_programplan.conflicts import analyze
from synaps_programplan.io.mspdi import ImportReport, read_mspdi, write_plan_mspdi
from synaps_programplan.model import TaskStatus
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.report import render_html, report_data
from synaps_programplan.result import Severity
from tests.conftest import program, task


def _started():
    return program(
        [
            task(
                "b",
                4,
                status=TaskStatus.IN_PROGRESS,
                actual_start=date(2026, 10, 1),
                remaining_wd=2,
                percent_complete=50,
                planned_start=date(2026, 10, 1),
                planned_finish=date(2026, 10, 6),
            )
        ]
    )


def test_the_solver_keeps_the_actual_start_and_forecasts_after_the_status_date() -> None:
    prog = _started()
    result = plan(prog, SolveConfig(solver="greedy"))
    assert result.outcome.ok
    row = result.task("b")
    assert row.start == date(2026, 10, 1)
    assert row.finish >= prog.program.planning_start


def test_a_moved_start_and_an_early_forecast_are_rejected() -> None:
    prog = _started()
    result = plan(prog, SolveConfig(solver="greedy"))
    row = result.task("b")
    moved = row.model_copy(update={"start": date(2026, 10, 5)})
    codes = {item.code for item in check_plan(prog, [moved]) if item.severity is Severity.HARD}
    assert "STARTED_MOVED" in codes
    early = row.model_copy(update={"finish": date(2026, 10, 2)})
    codes = {item.code for item in check_plan(prog, [early]) if item.severity is Severity.HARD}
    assert "FORECAST_BEFORE_STATUS" in codes


def test_the_report_names_baseline_actual_and_forecast() -> None:
    prog = _started()
    result = plan(prog, SolveConfig(solver="greedy"))
    assert result.outcome.ok
    payload = report_data(prog, [result], analysis=analyze(prog))
    row = payload["scenarios"][0]["tasks"][0]
    assert row["percent"] == 50
    assert row["actual_start"] == "2026-10-01"
    assert row["forecast_start"] == prog.program.planning_start.isoformat()
    assert row["forecast_finish"] == result.task("b").finish.isoformat()
    assert row["forecast_start"] <= row["forecast_finish"]
    page_bits = ("факт и прогноз", "База:", "Прогноз:")
    html = render_html(payload, "проверка")
    for bit in page_bits:
        assert bit in html


def test_mspdi_roundtrip_keeps_actuals_without_losses(tmp_path: Path) -> None:
    prog = _started()
    result = plan(prog, SolveConfig(solver="greedy"))
    assert result.outcome.ok
    out = tmp_path / "plan.xml"
    write_plan_mspdi(prog, result, out)
    report = ImportReport()
    imported = read_mspdi(out, code="p1", report=report)
    assert report.notes == []
    row = imported.tasks[0]
    assert row.status is TaskStatus.IN_PROGRESS
    assert row.actual_start == date(2026, 10, 1)
    assert row.percent_complete == 50
    assert row.remaining_wd == 2
    assert row.actual_finish is None
