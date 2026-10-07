"""Values an importer had to round are recorded as losses; whole values are not."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from synaps_programplan.io.mspdi import NS, ImportReport
from synaps_programplan.io.mspdi import _task as mspdi_task
from synaps_programplan.io.xer import _task as xer_task


def _codes(report: ImportReport) -> list[tuple[str, str]]:
    return [(item.code, item.action) for item in report.losses]


def _xer_row(**extra: str) -> dict[str, str]:
    row = {
        "task_id": "9",
        "proj_id": "1",
        "task_code": "T1",
        "task_name": "T1",
        "task_type": "TT_Task",
        "target_drtn_hr_cnt": "160",
        "clndr_id": "c",
        "status_code": "TK_NotStart",
    }
    row.update(extra)
    return row


def test_a_whole_rounding_is_not_a_loss() -> None:
    report = ImportReport()
    report.rounded(2.0, 2, label="длительность", object_id="t")
    assert report.losses == []


def test_a_fractional_rounding_is_an_approximated_loss() -> None:
    report = ImportReport()
    report.rounded(2.5, 2, label="длительность", object_id="t")
    assert _codes(report) == [("DURATION_ROUNDED", "approximated")]


def test_xer_duration_of_two_and_a_half_days_is_reported() -> None:
    report = ImportReport()
    task = xer_task(_xer_row(target_drtn_hr_cnt="20"), "p.T1", "p", 8.0, set(), [], report)
    assert task.duration_wd == 2
    assert _codes(report) == [("DURATION_ROUNDED", "approximated")]


def test_xer_remaining_is_unknown_when_the_field_is_empty() -> None:
    report = ImportReport()
    task = xer_task(
        _xer_row(status_code="TK_Active", act_start_date="2026-10-05 08:00", remain_drtn_hr_cnt=""),
        "p.T1",
        "p",
        8.0,
        set(),
        [],
        report,
    )
    assert task.remaining_wd is None


def test_xer_finish_on_is_reported_as_approximated() -> None:
    report = ImportReport()
    xer_task(_xer_row(cstr_type="CS_MEO", cstr_date="2026-12-01 00:00"), "p.T1", "p", 8.0, set(), [], report)
    assert ("CONSTRAINT_APPROXIMATED", "approximated") in _codes(report)


def test_xer_finish_on_or_before_is_exact_and_silent() -> None:
    report = ImportReport()
    task = xer_task(
        _xer_row(cstr_type="CS_MEOB", cstr_date="2026-12-01 00:00"), "p.T1", "p", 8.0, set(), [], report
    )
    assert task.latest_finish is not None
    assert report.losses == []


def test_mspdi_duration_of_two_and_a_half_days_is_reported() -> None:
    node = ET.fromstring(
        f'<Task xmlns="{NS}"><UID>7</UID><Name>T7</Name><Duration>PT20H0M0S</Duration>'
        "<Start>2026-10-05T08:00:00</Start><Finish>2026-10-07T17:00:00</Finish></Task>"
    )
    report = ImportReport()
    task, _duration = mspdi_task(node, None, "p", "", 480.0, report, False)
    assert task.duration_wd == 2
    assert _codes(report) == [("DURATION_ROUNDED", "approximated")]
