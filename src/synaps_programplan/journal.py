"""Operator decision journal (pilot requirement: accept / reject with reason).

Append-only JSON Lines. Every record carries the hash of the previous record
and its own hash over the canonical JSON, so a deleted, reordered or edited
line breaks the chain and ``verify_journal`` reports where.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
ACTIONS = ("accept", "reject", "check", "repair")


def _digest(record: dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "hash"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_decision(
    path: Path,
    *,
    action: str,
    user: str,
    role: str,
    scenario_id: str,
    plan_hash: str | None,
    input_hash: str | None,
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    records = read_journal(path)
    record: dict[str, Any] = {
        "seq": len(records) + 1,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "user": user,
        "role": role,
        "action": action,
        "scenario_id": scenario_id,
        "plan_hash": plan_hash,
        "input_hash": input_hash,
        "reason": reason,
        "details": details or {},
        "prev": records[-1]["hash"] if records else GENESIS,
    }
    record["hash"] = _digest(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


@dataclass(frozen=True)
class JournalCheck:
    ok: bool
    records: int
    broken_at: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "records": self.records, "broken_at": self.broken_at, "reason": self.reason}


def verify_journal(path: Path) -> JournalCheck:
    records = read_journal(path)
    previous = GENESIS
    for index, record in enumerate(records, start=1):
        if record.get("seq") != index:
            return JournalCheck(False, len(records), index, "sequence number out of order")
        if record.get("prev") != previous:
            return JournalCheck(False, len(records), index, "previous-hash link is broken")
        if record.get("hash") != _digest(record):
            return JournalCheck(False, len(records), index, "record content does not match its hash")
        previous = record["hash"]
    return JournalCheck(True, len(records))
