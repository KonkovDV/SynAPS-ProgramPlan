"""Every path that can emit plan dates calls the publish gate by name."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = {"attestation_error", "require_attestation"}


def _tree(relative: str) -> ast.AST:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def _direct_calls(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                found.add(func.id)
            elif isinstance(func, ast.Attribute):
                found.add(func.attr)
        found |= _direct_calls(child)
    return found


def _only(tree: ast.AST, name: str) -> ast.FunctionDef:
    found = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(found) == 1, name
    return found[0]


def _method(tree: ast.AST, class_name: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == name:
                return item
    raise AssertionError(f"{class_name}.{name}")


def _nested(tree: ast.AST, parent: str, name: str) -> ast.FunctionDef:
    host = _only(tree, parent)
    found = [
        node
        for node in ast.walk(host)
        if isinstance(node, ast.FunctionDef) and node.name == name and node is not host
    ]
    assert len(found) == 1, name
    return found[0]


def test_every_date_exit_calls_the_publish_gate() -> None:
    cli = _tree("src/synaps_programplan/cli.py")
    api = _tree("src/synaps_programplan/api.py")
    report = _tree("src/synaps_programplan/report.py")
    workbench = _tree("src/synaps_programplan/workbench.py")
    gated = [
        _only(cli, "cmd_check"),
        _only(cli, "cmd_export"),
        _only(cli, "cmd_risk"),
        _only(cli, "cmd_demo"),
        _only(cli, "_risk_for"),
        _only(api, "solve_program"),
        _only(api, "check"),
        _only(api, "risk"),
        _only(report, "report_data"),
        _method(workbench, "Workbench", "accepted"),
        _nested(workbench, "create_app", "repair"),
    ]
    missing = [fn.name for fn in gated if not GATE & _direct_calls(fn)]
    assert not missing, missing
    assert "build_report" in _direct_calls(_only(cli, "cmd_report"))
    assert "report_data" in _direct_calls(_only(report, "build_report"))
    assert "report_data" in _direct_calls(_method(workbench, "Workbench", "data"))
    assert "accepted" in _direct_calls(_nested(workbench, "create_app", "plan_json"))
    assert "accepted" in _direct_calls(_nested(workbench, "create_app", "plan_mspdi"))
    assert "accepted" in _direct_calls(_nested(workbench, "create_app", "check"))
