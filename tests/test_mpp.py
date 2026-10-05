from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from synaps_programplan.io.mpp import read_mpp
from synaps_programplan.io.mspdi import read_mspdi
from synaps_programplan.model import DependencyType, TaskKind
from tests.test_mspdi import _XML

pytest.importorskip("mpxj")
pytest.importorskip("jpype")


def test_mpp_reader_matches_the_mspdi_import(tmp_path: Path) -> None:
    xml = tmp_path / "okr.xml"
    xml.write_text(_XML, encoding="utf-8")
    via_reader = read_mpp(xml, code="okr1")
    via_mspdi = read_mspdi(xml, code="okr1")
    assert via_reader.name == via_mspdi.name == "ОКР «Изделие»"
    assert [(t.name, t.duration_wd, t.kind, t.deadline) for t in via_reader.tasks] == [
        (t.name, t.duration_wd, t.kind, t.deadline) for t in via_mspdi.tasks
    ]
    assert via_reader.tasks[0].duration_wd == 3
    assert via_reader.tasks[0].demands[0].units == 10
    assert via_reader.tasks[1].kind is TaskKind.MILESTONE
    assert via_reader.tasks[1].deadline == date(2026, 10, 15)
    assert via_reader.dependencies[0].type is DependencyType.FS
    assert {node.name for node in via_reader.wbs} == {"Этап"}
    assert {r.name for r in via_reader.resources} == {"Иванов"}


def test_mpx_roundtrip_keeps_tasks_links_and_resources(tmp_path: Path) -> None:
    import jpype

    xml = tmp_path / "okr.xml"
    xml.write_text(_XML, encoding="utf-8")
    if not jpype.isJVMStarted():
        jpype.startJVM()
    from org.mpxj.mpx import MPXWriter
    from org.mpxj.reader import UniversalProjectReader

    project = UniversalProjectReader().read(str(xml))
    mpx = tmp_path / "okr.mpx"
    MPXWriter().write(project, str(mpx))
    imported = read_mpp(mpx, code="okr1")
    assert [t.duration_wd for t in imported.tasks] == [3, 0]
    assert imported.dependencies[0].src_task_id.endswith(".2")
    assert imported.dependencies[0].type is DependencyType.FS
    assert imported.resources[0].capacity_units == 10
    assert imported.tasks[0].demands[0].units == 10
