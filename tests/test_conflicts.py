from __future__ import annotations

from datetime import date

from synaps_programplan.conflicts import analyze
from tests.conftest import dep, program, stand, task, uses


def test_source_plan_stand_contention_and_broken_link() -> None:
    prog = program(
        [
            task(
                "a",
                5,
                "p1",
                demands=uses("st"),
                planned_start=date(2026, 10, 5),
                planned_finish=date(2026, 10, 9),
            ),
            task(
                "b",
                5,
                "p2",
                demands=uses("st"),
                planned_start=date(2026, 10, 5),
                planned_finish=date(2026, 10, 9),
            ),
        ],
        [dep("a", "b")],
        resources=[stand()],
    )
    summary = analyze(prog).summary()
    assert summary["SHARED_CONTENTION"] >= 1
    assert summary["LINK_BROKEN"] == 1
