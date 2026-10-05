"""Stable hashing, provenance and evidence stamps."""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import date, datetime
from enum import Enum
from importlib import metadata
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from synaps_programplan.versions import CLAIM_LEVEL, ISO16290_TRL, NAME, SYNAPS_COMMIT, VERSION


def runtime_manifest() -> dict[str, str]:
    versions = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "name": NAME,
        "version": VERSION,
        "synaps_commit": SYNAPS_COMMIT,
    }
    for dist in ("ortools", "pydantic"):
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[dist] = "absent"
    return versions


def to_canonical(data: Any) -> Any:
    """JSON-ready value; refuses types that ``default=str`` would hide."""
    if isinstance(data, BaseModel):
        return to_canonical(data.model_dump(mode="json"))
    if isinstance(data, datetime | date):
        return data.isoformat()
    if isinstance(data, UUID):
        return str(data)
    if isinstance(data, Enum):
        return to_canonical(data.value)
    if isinstance(data, dict):
        return {str(key): to_canonical(value) for key, value in data.items()}
    if isinstance(data, list | tuple | frozenset | set):
        items = [to_canonical(value) for value in data]
        return sorted(items, key=str) if isinstance(data, frozenset | set) else items
    if data is None or isinstance(data, str | int | bool):
        return data
    if isinstance(data, float):
        if data != data or data in {float("inf"), float("-inf")}:
            raise TypeError("non-finite float is not canonical")
        return data
    raise TypeError(f"cannot canonicalize {type(data).__name__}")


def canonical_json(data: Any) -> str:
    return json.dumps(to_canonical(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def evidence_stamp(
    *,
    input_hash: str,
    config: dict[str, Any],
    data_provenance: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": NAME,
        "version": VERSION,
        "synaps_commit": SYNAPS_COMMIT,
        "claim_level": CLAIM_LEVEL,
        "iso16290_trl": ISO16290_TRL,
        "data_provenance": data_provenance,
        "input_hash": input_hash,
        "config": config,
        "config_hash": fingerprint(config),
        "runtime": runtime_manifest(),
    }
    if extra:
        payload.update(extra)
    return payload
