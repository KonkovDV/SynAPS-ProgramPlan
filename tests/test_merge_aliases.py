from __future__ import annotations

from datetime import date

from synaps_programplan.io.mspdi import ImportedProject
from synaps_programplan.merge import merge_projects
from synaps_programplan.model import Resource, ResourceKind


def _project(code: str, resource_name: str) -> ImportedProject:
    return ImportedProject(
        code=code,
        name=code,
        tasks=[],
        wbs=[],
        dependencies=[],
        resources=[
            Resource(
                id=f"{code}.r",
                kind=ResourceKind.STAND,
                code=resource_name,
                name=resource_name,
                capacity_units=10,
            )
        ],
        status_date=None,
        start=date(2026, 10, 1),
        finish=date(2026, 12, 1),
    )


def test_alias_table_merges_resources_the_names_would_not() -> None:
    program, report = merge_projects(
        [_project("a", "ТС-1"), _project("b", "Стенд термостатирования")],
        program_id="p",
        name="Программа",
        aliases={"тс-1": "Стенд термостатирования"},
    )
    assert len(program.resources) == 1
    assert program.resources[0].name == "Стенд термостатирования"
    assert set(report.shared_resources[program.resources[0].id]) == {"a", "b"}
    assert any(item.code == "ALIAS_APPLIED" and item.action == "info" for item in report.losses)


def test_similar_names_are_reported_and_not_merged() -> None:
    program, report = merge_projects(
        [_project("a", "Испытательный стенд ТС-1"), _project("b", "Испытательный стенд ТС1")],
        program_id="p",
        name="Программа",
    )
    assert len(program.resources) == 2
    assert any(item.code == "SIMILAR_RESOURCE" and item.action == "info" for item in report.losses)
