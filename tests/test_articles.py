from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from synaps_programplan.io.excel import read_excel, write_excel
from synaps_programplan.model import Product, ProductConfiguration, Resource, ResourceKind
from synaps_programplan.model import TestArticle as Prototype
from synaps_programplan.planner import SolveConfig, plan
from tests.conftest import program, task, uses


def _family(capacity: int = 1) -> dict:
    return {
        "resources": [
            Resource(
                id="eng",
                kind=ResourceKind.EQUIPMENT,
                code="ENG",
                name="экземпляр",
                capacity_units=capacity,
            )
        ],
        "products": [Product(id="pd", project_id="p1", code="PD", name="изделие")],
        "configurations": [ProductConfiguration(id="cfg", product_id="pd", code="BASE", name="базовая")],
        "articles": [
            Prototype(id="e1", configuration_id="cfg", code="E1", name="опытный 1", resource_id="eng")
        ],
    }


def test_one_prototype_cannot_take_two_tests_at_once() -> None:
    family = _family()
    prog = program(
        [
            task("t1", 3, demands=uses("eng"), test_article_id="e1"),
            task("t2", 3, demands=uses("eng"), test_article_id="e1"),
        ],
        **family,
    )
    result = plan(prog, SolveConfig(solver="greedy", time_limit_s=10))
    assert result.outcome.ok
    starts = sorted(row.start_index for row in result.tasks)
    assert starts == [0, 3]


def test_an_unknown_article_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown test article"):
        program([task("a", 2, test_article_id="missing")])


def test_a_named_article_must_be_occupied() -> None:
    with pytest.raises(ValidationError, match="does not occupy"):
        program([task("a", 2, test_article_id="e1")], **_family())


def test_an_article_resource_wider_than_one_is_rejected() -> None:
    with pytest.raises(ValidationError, match="capacity 1"):
        program([task("a", 2, demands=uses("eng"), test_article_id="e1")], **_family(2))


def test_article_roundtrip_in_excel(tmp_path: Path) -> None:
    original = program([task("a", 2, demands=uses("eng"), test_article_id="e1")], **_family())
    path = tmp_path / "program.xlsx"
    write_excel(original, path)
    loaded = read_excel(path)
    assert loaded.articles[0].code == "E1"
    assert loaded.tasks[0].test_article_id == "e1"
    assert loaded.products[0].project_id == "p1"
