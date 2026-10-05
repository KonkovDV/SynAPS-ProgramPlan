from __future__ import annotations

from datetime import date
from pathlib import Path

from synaps_programplan.io.mspdi import ImportReport, read_mspdi, write_plan_mspdi
from synaps_programplan.merge import merge_projects
from synaps_programplan.model import DependencyType, TaskKind, TaskStatus
from synaps_programplan.planner import SolveConfig, plan

_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="http://schemas.microsoft.com/project">
  <Name>Demo</Name><Title>ОКР «Изделие»</Title>
  <MinutesPerDay>480</MinutesPerDay>
  <StartDate>2026-10-05T08:00:00</StartDate>
  <FinishDate>2026-10-30T18:00:00</FinishDate>
  <Tasks>
    <Task><UID>0</UID><Name>root</Name><Summary>1</Summary><OutlineLevel>0</OutlineLevel></Task>
    <Task><UID>1</UID><Name>Этап</Name><Summary>1</Summary><OutlineLevel>1</OutlineLevel>
      <Start>2026-10-05T08:00:00</Start><Finish>2026-10-09T17:00:00</Finish></Task>
    <Task><UID>2</UID><Name>Работа</Name><Summary>0</Summary><OutlineLevel>2</OutlineLevel>
      <Start>2026-10-05T08:00:00</Start><Finish>2026-10-07T17:00:00</Finish>
      <Duration>PT24H0M0S</Duration><Milestone>0</Milestone></Task>
    <Task><UID>3</UID><Name>Веха</Name><Summary>0</Summary><OutlineLevel>2</OutlineLevel>
      <Milestone>1</Milestone><Duration>PT0H0M0S</Duration>
      <Start>2026-10-07T17:00:00</Start><Finish>2026-10-07T17:00:00</Finish>
      <Deadline>2026-10-15T17:00:00</Deadline>
      <PredecessorLink><PredecessorUID>2</PredecessorUID><Type>1</Type>
        <LinkLag>0</LinkLag><LagFormat>7</LagFormat></PredecessorLink></Task>
  </Tasks>
  <Resources>
    <Resource><UID>0</UID><Name>Unassigned</Name><Type>1</Type></Resource>
    <Resource><UID>1</UID><Name>Иванов</Name><Type>1</Type><MaxUnits>1</MaxUnits></Resource>
  </Resources>
  <Assignments>
    <Assignment><TaskUID>2</TaskUID><ResourceUID>1</ResourceUID><Units>1</Units></Assignment>
  </Assignments>
</Project>
"""


def test_mspdi_import_and_merge(tmp_path: Path) -> None:
    path = tmp_path / "okr.xml"
    path.write_text(_XML, encoding="utf-8")
    report = ImportReport()
    imported = read_mspdi(path, code="okr1", report=report)
    assert imported.name == "ОКР «Изделие»"
    work = next(t for t in imported.tasks if t.name == "Работа")
    milestone = next(t for t in imported.tasks if t.name == "Веха")
    assert work.duration_wd == 3
    assert work.demands[0].units == 10
    assert milestone.kind is TaskKind.MILESTONE
    assert milestone.deadline == date(2026, 10, 15)
    link = imported.dependencies[0]
    assert link.src_task_id == "okr1.2" and link.dst_task_id == "okr1.3"
    assert link.type is DependencyType.FS
    assert report.notes == []
    program, merge = merge_projects([imported], program_id="p", name="Программа")
    assert merge.capacity_conflicts == []
    assert {r.name for r in program.resources} == {"Иванов"}
    assert program.program.status_date == date(2026, 10, 5)


def test_plan_roundtrip_mspdi(tmp_path: Path) -> None:
    from tests.conftest import dep, program, task

    prog = program([task("a", 2), task("m", 0)], [dep("a", "m")])
    result = plan(prog, SolveConfig(time_limit_s=10))
    assert result.outcome.ok
    out = tmp_path / "plan.xml"
    write_plan_mspdi(prog, result, out)
    text = out.read_text(encoding="utf-8")
    assert "PredecessorUID" in text
    assert result.task("a").status is not TaskStatus.DONE
