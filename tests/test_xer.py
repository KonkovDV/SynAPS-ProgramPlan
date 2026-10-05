from __future__ import annotations

from datetime import date
from pathlib import Path

from synaps_programplan.cli import main
from synaps_programplan.io import load_program
from synaps_programplan.io.mspdi import ImportReport
from synaps_programplan.io.xer import parse_xer, read_xer
from synaps_programplan.merge import merge_projects
from synaps_programplan.model import DependencySource, DependencyType, ResourceKind, TaskStatus
from synaps_programplan.planner import SolveConfig, plan


def _table(name: str, fields: list[str], rows: list[list[str]]) -> list[str]:
    return [f"%T\t{name}", "%F\t" + "\t".join(fields)] + ["%R\t" + "\t".join(row) for row in rows]


TASK_FIELDS = [
    "task_id", "proj_id", "wbs_id", "clndr_id", "task_code", "task_name", "task_type", "status_code",
    "target_drtn_hr_cnt", "remain_drtn_hr_cnt", "act_start_date", "act_end_date", "early_start_date",
    "early_end_date", "cstr_type", "cstr_date",
]  # fmt: skip


def sample_xer() -> str:
    lines = ["ERMHDR\t19.12\t2026-10-01\tProject\tadmin\tdb\tProject Management\tRUB"]
    lines += _table(
        "PROJECT",
        ["proj_id", "proj_short_name", "last_recalc_date", "plan_start_date", "scd_end_date"],
        [["1", "OKR-A", "2026-10-05 08:00", "2026-09-01 08:00", "2027-03-01 17:00"],
         ["2", "OKR-B", "2026-10-05 08:00", "2026-10-05 08:00", "2027-04-01 17:00"]],
    )  # fmt: skip
    lines += _table("CALENDAR", ["clndr_id", "clndr_name", "day_hr_cnt"], [["10", "Standard", "8"]])
    lines += _table(
        "PROJWBS",
        ["wbs_id", "proj_id", "parent_wbs_id", "wbs_short_name", "wbs_name", "proj_node_flag"],
        [["100", "1", "", "OKR-A", "OKR-A", "Y"], ["101", "1", "100", "TP", "Технический проект", "N"],
         ["200", "2", "", "OKR-B", "OKR-B", "Y"], ["201", "2", "200", "PI", "Испытания", "N"]],
    )  # fmt: skip
    lines += _table(
        "TASK",
        TASK_FIELDS,
        [
            ["1001", "1", "101", "10", "A10", "Эскиз", "TT_Task", "TK_Complete", "80", "0",
             "2026-09-01 08:00", "2026-09-14 17:00", "", "", "", ""],
            ["1002", "1", "101", "10", "A20", "Расчёты", "TT_Task", "TK_Active", "80", "40",
             "2026-09-28 08:00", "", "2026-09-28 08:00", "2026-10-09 17:00", "", ""],
            ["1003", "1", "101", "10", "A30", "Выпуск ТП", "TT_FinMile", "TK_NotStart", "0", "0",
             "", "", "2026-10-09 17:00", "2026-10-09 17:00", "CS_MEOB", "2026-12-30 17:00"],
            ["1004", "1", "101", "10", "A40", "Сопровождение", "TT_LOE", "TK_NotStart", "400", "400",
             "", "", "", "", "", ""],
            ["2001", "2", "201", "10", "B10", "Испытания на стенде", "TT_Task", "TK_NotStart", "120", "120",
             "", "", "2026-10-12 08:00", "2026-10-26 17:00", "CS_MSOA", "2026-10-12 08:00"],
            ["2002", "2", "201", "10", "B20", "Отчёт", "TT_Task", "TK_NotStart", "16", "16",
             "", "", "2026-10-27 08:00", "2026-10-28 17:00", "CS_ALAP", ""],
        ],
    )  # fmt: skip
    lines += _table(
        "TASKPRED",
        ["task_pred_id", "task_id", "pred_task_id", "proj_id", "pred_proj_id", "pred_type", "lag_hr_cnt"],
        [["1", "1002", "1001", "1", "1", "PR_FS", "0"], ["2", "1003", "1002", "1", "1", "PR_FS", "0"],
         ["3", "2001", "1003", "2", "1", "PR_FS", "16"], ["4", "2002", "2001", "2", "2", "PR_SS", "40"]],
    )  # fmt: skip
    lines += _table(
        "RSRC",
        ["rsrc_id", "rsrc_name", "rsrc_short_name", "rsrc_type"],
        [["7", "Стенд ТС-1", "TS1", "RT_Equip"], ["8", "Инженер-испытатель", "ENG", "RT_Labor"],
         ["9", "Кабель", "CAB", "RT_Mat"]],
    )  # fmt: skip
    lines += _table("RSRCRATE", ["rsrc_id", "start_date", "max_qty_per_hr"], [["8", "2026-01-01", "2"]])
    lines += _table(
        "TASKRSRC",
        ["task_id", "rsrc_id", "proj_id", "target_qty_per_hr"],
        [["2001", "7", "2", "1"], ["2001", "8", "2", "1"], ["1002", "8", "1", "0.5"]],
    )
    lines.append("%E")
    return "\r\n".join(lines) + "\r\n"


def _write(tmp_path: Path, encoding: str = "cp1251") -> Path:
    path = tmp_path / "program.xer"
    path.write_bytes(sample_xer().encode(encoding))
    return path


def test_parse_tables() -> None:
    tables = parse_xer(sample_xer())
    assert len(tables["TASK"]) == 6 and tables["PROJECT"][1]["proj_short_name"] == "OKR-B"


def test_two_projects_with_a_native_cross_link(tmp_path: Path) -> None:
    report = ImportReport()
    projects, cross = read_xer(_write(tmp_path), report=report)
    assert [p.code for p in projects] == ["OKR-A", "OKR-B"]
    a, b = projects
    tasks = {t.id: t for t in a.tasks + b.tasks}
    assert "OKR-A.A40" not in tasks and any("TT_LOE" in n for n in report.notes)
    assert tasks["OKR-A.A10"].status is TaskStatus.DONE
    active = tasks["OKR-A.A20"]
    assert active.status is TaskStatus.IN_PROGRESS and active.remaining_wd == 5
    assert tasks["OKR-A.A30"].is_milestone and tasks["OKR-A.A30"].latest_finish == date(2026, 12, 30)
    assert tasks["OKR-B.B10"].earliest_start == date(2026, 10, 12) and tasks["OKR-B.B10"].duration_wd == 15
    assert any("CS_ALAP" in n for n in report.notes)
    assert len(cross) == 1 and cross[0].source is DependencySource.CROSS_PROJECT and cross[0].lag_wd == 2
    ss = next(d for d in b.dependencies if d.type is DependencyType.SS)
    assert ss.lag_wd == 5
    kinds = {r.code: r.kind for r in b.resources}
    assert kinds == {"TS1": ResourceKind.EQUIPMENT, "ENG": ResourceKind.GROUP}
    assert any("Кабель" in n for n in report.notes)
    assert {n.name for n in a.wbs} == {"Технический проект"} and a.wbs[0].parent_id is None


def test_merged_program_shares_the_engineer_and_solves(tmp_path: Path) -> None:
    projects, cross = read_xer(_write(tmp_path, "utf-8"))
    merged, merge = merge_projects(projects, program_id="p", name="XER", links=cross)
    assert merge.shared_resources and list(merge.shared_resources.values())[0] == ["OKR-A", "OKR-B"]
    result = plan(merged, SolveConfig(time_limit_s=5))
    assert result.outcome.ok, result.outcome.detail
    assert result.task("OKR-B.B10").start > result.task("OKR-A.A30").finish


def test_cli_imports_xer(tmp_path: Path) -> None:
    out = tmp_path / "program.json"
    assert main(["import", str(_write(tmp_path)), "--out", str(out), "--provenance", "experiment"]) == 0
    program = load_program(out)
    assert len(program.projects) == 2
    assert any(d.source is DependencySource.CROSS_PROJECT for d in program.dependencies)
    assert program.provenance.source_file_hash
