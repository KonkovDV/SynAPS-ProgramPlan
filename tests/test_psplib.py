from __future__ import annotations

from pathlib import Path

import pytest

from synaps_programplan.checker import check_plan
from synaps_programplan.io.psplib import (
    InstanceInfeasible,
    TemporallyInfeasible,
    load_sch,
    load_sm,
    max_to_program,
    parse_sch,
    parse_sm,
    read_max_reference,
    read_optimum,
    to_program,
)
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.result import Severity

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


# ProGen/max PSP1 of test set j10 (published optimum 26).
PSP1 = """10\t5\t0\t0
0\t1\t4\t4\t2\t1\t3\t[0]\t[0]\t[0]\t[0]
1\t1\t4\t9\t7\t8\t10\t[9]\t[1]\t[8]\t[2]
2\t1\t1\t8\t[24]
3\t1\t2\t10\t7\t[4]\t[8]
4\t1\t3\t10\t9\t5\t[0]\t[0]\t[7]
5\t1\t1\t6\t[0]
6\t1\t1\t11\t[5]
7\t1\t1\t11\t[10]
8\t1\t3\t1\t2\t11\t[-22]\t[-34]\t[2]
9\t1\t1\t11\t[6]
10\t1\t1\t11\t[1]
11\t1\t0
0\t1\t0\t0\t0\t0\t0\t0
1\t1\t3\t4\t1\t0\t0\t0
2\t1\t10\t1\t0\t3\t0\t0
3\t1\t3\t4\t0\t2\t2\t3
4\t1\t3\t0\t0\t0\t3\t0
5\t1\t3\t0\t1\t2\t4\t0
6\t1\t5\t2\t3\t4\t0\t0
7\t1\t10\t0\t4\t4\t0\t4
8\t1\t2\t2\t0\t0\t4\t4
9\t1\t6\t5\t0\t0\t1\t1
10\t1\t1\t0\t1\t0\t0\t0
11\t1\t0\t0\t0\t0\t0\t0
5\t5\t5\t5\t5
"""


def test_rcpsp_max_instance_reaches_the_published_optimum() -> None:
    instance = parse_sch(PSP1, name="psp1")
    assert instance.jobs == 12 and instance.capacities == [5] * 5
    assert (8, 1, -22) in instance.lags
    program = max_to_program(instance)
    back = [d for d in program.dependencies if d.max_lag_wd is not None]
    assert back, "negative lags become maximum lags of the forward link"
    result = plan(program, SolveConfig(time_limit_s=10, objective="finish"))
    assert result.outcome.ok and result.outcome.claim.value == "OPTIMAL"
    assert result.task("end").end_index == 26
    starts = {row.task_id: row.start_index for row in result.tasks}
    for src, dst, lag in instance.lags:
        if src not in (0, 11) and dst not in (0, 11):
            assert starts[f"a{dst}"] - starts[f"a{src}"] >= lag
    assert not [v for v in check_plan(program, result.tasks) if v.severity is Severity.HARD]


def test_rcpsp_max_infeasibility_is_proven_not_planned() -> None:
    cycle = PSP1.replace("6\t1\t1\t11\t[5]", "6\t1\t2\t11\t5\t[5]\t[1]")
    instance = parse_sch(cycle, name="cycle")
    assert (6, 5, 1) in instance.lags
    with pytest.raises(TemporallyInfeasible):
        max_to_program(instance)
    # Two unit-capacity jobs of 3 days that must start at most 1 day apart.
    overlap = "2 1 0 0\n0 1 2 1 2 [0] [0]\n1 1 2 2 3 [0] [3]\n2 1 2 1 3 [-1] [3]\n3 1 0\n"
    overlap += "0 1 0 0\n1 1 3 1\n2 1 3 1\n3 1 0 0\n1\n"
    program = max_to_program(parse_sch(overlap, name="overlap"))
    assert [(d.lag_wd, d.max_lag_wd) for d in program.dependencies if d.dst_task_id == "a2"] == [(0, 1)]
    result = plan(program, SolveConfig(time_limit_s=10, objective="finish"))
    assert not result.outcome.ok and result.outcome.claim.value == "INFEASIBLE"


def test_demand_above_capacity_is_proven_infeasible() -> None:
    text = "1 1 0 0\n0 1 1 2 [0]\n1 1 1 2 [1]\n2 1 0\n0 1 0 0\n1 1 1 5\n2 1 0 0\n2\n"
    with pytest.raises(InstanceInfeasible, match="capacity is 2"):
        max_to_program(parse_sch(text, name="wide"))


def test_rcpsp_max_reference_table(tmp_path: Path) -> None:
    path = tmp_path / "optimum.csv"
    path.write_text("problem,optimum\nPSP1.SCH,26\nPSP2.SCH,unsat\npsp3.sch,59..95\n", encoding="utf-8")
    ref = read_max_reference(path)
    assert ref["psp1"].lower == ref["psp1"].upper == 26
    assert ref["psp2"].infeasible
    assert (ref["psp3"].lower, ref["psp3"].upper) == (59, 95)
    sch = tmp_path / "PSP1.SCH"
    sch.write_text(PSP1, encoding="utf-8")
    assert load_sch(sch).program.id == "PSP1"
