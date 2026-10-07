"""What changed between two versions of one programme."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from synaps_programplan.model import OKRProgram

_TASK_FIELDS = (
    "name",
    "project_id",
    "duration_wd",
    "kind",
    "status",
    "pinned",
    "shift_limit_wd",
    "remaining_wd",
    "percent_complete",
    "demands",
    "modes",
    "earliest_start",
    "latest_finish",
    "due_date",
    "deadline",
    "actual_start",
    "actual_finish",
    "planned_start",
    "planned_finish",
    "baseline_start",
    "baseline_finish",
)
_TASK_DATES = frozenset(
    {
        "earliest_start",
        "latest_finish",
        "due_date",
        "deadline",
        "actual_start",
        "actual_finish",
        "planned_start",
        "planned_finish",
        "baseline_start",
        "baseline_finish",
    }
)
_PROJECT_FIELDS = ("name", "code", "priority", "customer", "enterprise", "due_date", "deadline")
_PROJECT_DATES = frozenset({"due_date", "deadline"})
_RESOURCE_FIELDS = ("code", "name", "kind", "capacity_units", "calendar_id", "skills")
_LINK_FIELDS = ("lag_wd", "max_lag_wd", "hard", "source")


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _changes(
    before: dict[str, Any], after: dict[str, Any], fields: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    changed: dict[str, dict[str, Any]] = {}
    for name in fields:
        left = before.get(name)
        right = after.get(name)
        if left != right:
            changed[name] = {"before": left, "after": right}
    return changed


def _by_id(items: list[Any]) -> dict[str, Any]:
    return {item.id: item for item in items}


def _id_diff(before: list[Any], after: list[Any], fields: tuple[str, ...]) -> dict[str, list[Any]]:
    left = _by_id(before)
    right = _by_id(after)
    changed = []
    for ident in sorted(set(left) & set(right)):
        fields_changed = _changes(_dump(left[ident]), _dump(right[ident]), fields)
        if fields_changed:
            changed.append({"id": ident, "fields": fields_changed})
    return {
        "added": sorted(set(right) - set(left)),
        "removed": sorted(set(left) - set(right)),
        "changed": changed,
    }


def _link_key(raw: dict[str, Any]) -> tuple[str, str, str]:
    return (raw["src_task_id"], raw["dst_task_id"], raw["type"])


def _link_label(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "src": raw["src_task_id"],
        "dst": raw["dst_task_id"],
        "type": raw["type"],
        "lag_wd": raw["lag_wd"],
    }


def _link_diff(before: list[Any], after: list[Any]) -> dict[str, list[Any]]:
    def grouped(items: list[Any]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
        buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in items:
            raw = _dump(item)
            buckets.setdefault(_link_key(raw), []).append(raw)
        return buckets

    left = grouped(before)
    right = grouped(after)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        old = left.get(key, [])
        new = right.get(key, [])
        for raw_before, raw_after in zip(old, new, strict=False):
            fields = _changes(raw_before, raw_after, _LINK_FIELDS)
            if fields:
                changed.append({**_link_label(raw_after), "fields": fields})
        for raw in old[len(new) :]:
            removed.append(_link_label(raw))
        for raw in new[len(old) :]:
            added.append(_link_label(raw))
    added.sort(key=lambda row: (row["src"], row["dst"], row["type"], row["lag_wd"]))
    removed.sort(key=lambda row: (row["src"], row["dst"], row["type"], row["lag_wd"]))
    return {"added": added, "removed": removed, "changed": changed}


def _date_rows(section: str, rows: list[dict[str, Any]], date_fields: frozenset[str]) -> list[dict[str, Any]]:
    found = []
    for row in rows:
        for field, pair in row["fields"].items():
            if field in date_fields:
                found.append({"entity": section, "id": row["id"], "field": field, **pair})
    return found


def diff_programs(before: OKRProgram, after: OKRProgram) -> dict[str, Any]:
    """Tasks, links, resources and dates that differ. Order is stable."""
    tasks = _id_diff(before.tasks, after.tasks, _TASK_FIELDS)
    projects = _id_diff(before.projects, after.projects, _PROJECT_FIELDS)
    resources = _id_diff(before.resources, after.resources, _RESOURCE_FIELDS)
    dates = _date_rows("task", tasks["changed"], _TASK_DATES) + _date_rows(
        "project", projects["changed"], _PROJECT_DATES
    )
    dates.sort(key=lambda row: (row["entity"], row["id"], row["field"]))
    return {
        "tasks": tasks,
        "dependencies": _link_diff(before.dependencies, after.dependencies),
        "resources": resources,
        "projects": projects,
        "dates": dates,
    }
