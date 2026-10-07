"""Each schedule-quality code is produced by the violation it names."""

from __future__ import annotations

from datetime import date

from synaps_programplan.conflicts import analyze
from tests.conftest import dep, program, stand, task, uses

_MONDAY = date(2026, 10, 5)


def _codes(prog) -> set[str]:
    found = analyze(prog)
    return {item.kind.value for item in found.conflicts} | {item.code for item in found.quality}


def _linked(work: str, **extra):
    """A resourced task that leads to a milestone, so only the planted issue remains."""
    return program(
        [task(work, extra.pop("duration", 1), demands=uses("st"), **extra), task("m", 0)],
        [dep(work, "m")],
        resources=[stand()],
    )


def test_each_planted_violation_has_its_own_code() -> None:
    dangling = program([task("loose", 1, demands=uses("st"))], resources=[stand()])
    open_end = program(
        [task("a", 1, demands=uses("st")), task("tail", 1, demands=uses("st"))],
        [dep("a", "tail")],
        resources=[stand()],
    )
    no_resources = program([task("bare", 1), task("m", 0)], [dep("bare", "m")])
    long_task = _linked("long", duration=67)
    lead = program(
        [task("a", 1, demands=uses("st")), task("b", 0)],
        [dep("a", "b", lag=-1)],
        resources=[stand()],
    )
    high_lag = program(
        [task("a", 1, demands=uses("st")), task("b", 0)],
        [dep("a", "b", lag=21)],
        resources=[stand()],
    )
    hard = program(
        [task(f"h{i}", 1, demands=uses("st"), earliest_start=_MONDAY) for i in range(5)]
        + [task("hm", 0, earliest_start=_MONDAY)],
        [dep(f"h{i}", f"h{i + 1}") for i in range(4)] + [dep("h4", "hm")],
        resources=[stand()],
    )
    stale = _linked("old", planned_start=date(2026, 10, 1), planned_finish=date(2026, 10, 1))
    weekend = _linked("sat", planned_start=date(2026, 10, 10), planned_finish=date(2026, 10, 12))
    impossible = program(
        [task("a", 5, demands=uses("st")), task("late", 0, deadline=_MONDAY)],
        [dep("a", "late")],
        resources=[stand()],
    )
    expect = {
        "DANGLING": dangling,
        "OPEN_END": open_end,
        "NO_RESOURCES": no_resources,
        "LONG_TASK": long_task,
        "NEGATIVE_LAG": lead,
        "HIGH_LAG": high_lag,
        "HARD_CONSTRAINTS": hard,
        "STALE_PLANNED": stale,
        "NONWORKING_DATE": weekend,
        "DEADLINE_IMPOSSIBLE": impossible,
    }
    for code, prog in expect.items():
        assert _codes(prog) == {code}, code
