from __future__ import annotations

from datetime import date

from synaps_programplan.explanations import infeasibility_witness
from tests.conftest import program, stand, task, uses


def test_two_necessary_deadlines_say_any_one_relaxation_is_enough() -> None:
    prog = program(
        [
            task("a", 5, deadline=date(2026, 10, 9), demands=uses("st")),
            task("b", 5, deadline=date(2026, 10, 9), demands=uses("st")),
        ],
        resources=[stand()],
    )
    witness = infeasibility_witness(prog, time_limit_s=8)
    assert witness["infeasible"] is True
    assert witness["minimal"] is True
    assert len(witness["witness"]) == 2
    assert all(item["alone_sufficient"] is True for item in witness["witness"])
    assert witness["text"].endswith("Достаточно ослабить любое из них")
