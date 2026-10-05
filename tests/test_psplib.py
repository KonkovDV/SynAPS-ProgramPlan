from __future__ import annotations

from pathlib import Path

from synaps_programplan.io.psplib import load_sm, parse_sm, read_optimum, to_program
from synaps_programplan.planner import SolveConfig, plan

TINY = """************************************************************************
file with basedata            : tiny.bas
************************************************************************
projects                      :  1
jobs (incl. supersource/sink ):  5
horizon                       :  20
RESOURCES
  - renewable                 :  1   R
************************************************************************
PRECEDENCE RELATIONS:
jobnr.    #modes  #successors   successors
   1        1          3           2   3   4
   2        1          1           5
   3        1          1           5
   4        1          1           5
   5        1          0
************************************************************************
REQUESTS/DURATIONS:
jobnr. mode duration  R 1
------------------------------------------------------------------------
  1      1     0       0
  2      1     3       2
  3      1     2       2
  4      1     2       1
  5      1     0       0
************************************************************************
RESOURCEAVAILABILITIES:
  R 1
    2
************************************************************************
"""


def test_parse_and_solve_tiny_instance(tmp_path: Path) -> None:
    instance = parse_sm(TINY, name="tiny")
    assert instance.jobs == 5 and instance.capacities == [2] and instance.successors[1] == [2, 3, 4]
    program = to_program(instance)
    assert {t.id for t in program.tasks} == {"j2", "j3", "j4", "j5"}
    result = plan(program, SolveConfig(time_limit_s=5, objective="finish"))
    assert result.outcome.ok and result.outcome.claim.value == "OPTIMAL"
    assert result.task("j5").end_index == 7
    path = tmp_path / "tiny.sm"
    path.write_text(TINY, encoding="utf-8")
    assert load_sm(path).program.id == "tiny"


def test_optimum_tables(tmp_path: Path) -> None:
    sm = tmp_path / "j30opt.sm"
    sm.write_text("Par Inst Makespan CPU\n 1 1 43 0.1\n 48 10 50 0.2\n", encoding="utf-8")
    assert read_optimum(sm) == {"j301_1": 43, "j3048_10": 50}
    csv = tmp_path / "J30_BKS.csv"
    csv.write_text(
        "# Author(s);x\nID;Type;Value;Time;Solution\n1;optimal;43;10;0\n11;optimal;47;0;0\n", "utf-8"
    )
    assert read_optimum(csv) == {"j301_1": 43, "j302_1": 47}
