"""Operator decision journal (pilot requirement: accept / reject with reason).

Append-only JSON Lines. Every record carries the hash of the previous record
and its own hash over the canonical JSON, so a deleted, reordered or edited
line breaks the chain and ``verify_journal`` reports where.

A witness file next to the journal (``<journal>.head``) stores the record count
and the hash of the last record. Cutting the tail or replacing the file with a
fresh chain still verifies internally, but no longer matches the witness.
Replacing both files is not detectable here: pass the last hash, copied
somewhere the journal cannot rewrite, as ``anchor``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
ACTIONS = ("accept", "reject", "check", "repair")


def _digest(record: dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key not in {"hash", "sig"}}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _journal_key(key: str | None) -> str | None:
    if key is not None:
        return key or None
    value = os.environ.get("SYNAPS_PROGRAMPLAN_JOURNAL_KEY", "").strip()
    return value or None


def _sign(record_hash: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), record_hash.encode("ascii"), hashlib.sha256).hexdigest()


def witness_path(path: Path) -> Path:
    return path.with_name(path.name + ".head")


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_witness(path: Path) -> dict[str, Any] | None:
    witness = witness_path(path)
    if not witness.exists():
        return None
    loaded = json.loads(witness.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or "records" not in loaded or "hash" not in loaded:
        raise ValueError(f"{witness.name}: head witness is not a record count and a hash")
    return loaded


def _write_witness(path: Path, records: int, head: str) -> None:
    witness = witness_path(path)
    payload = {"records": records, "hash": head}
    temporary = witness.with_name(witness.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(witness)


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
    key: str | None = None,
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
    signing_key = _journal_key(key)
    if signing_key is not None:
        record["sig"] = _sign(record["hash"], signing_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    _write_witness(path, record["seq"], record["hash"])
    return record


@dataclass(frozen=True)
class JournalCheck:
    ok: bool
    records: int
    broken_at: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "records": self.records, "broken_at": self.broken_at, "reason": self.reason}


def _signature_rejected(record: dict[str, Any], key: str | None) -> bool:
    signing_key = _journal_key(key)
    if signing_key is None:
        return False
    signature = record.get("sig")
    expected = _sign(str(record.get("hash", "")), signing_key)
    return not isinstance(signature, str) or not hmac.compare_digest(signature, expected)


def _verify_chain(path: Path, key: str | None = None) -> JournalCheck:
    records = read_journal(path)
    previous = GENESIS
    for index, record in enumerate(records, start=1):
        if record.get("seq") != index:
            return JournalCheck(False, len(records), index, "sequence number out of order")
        if record.get("prev") != previous:
            return JournalCheck(False, len(records), index, "previous-hash link is broken")
        if record.get("hash") != _digest(record):
            return JournalCheck(False, len(records), index, "record content does not match its hash")
        if _signature_rejected(record, key):
            return JournalCheck(False, len(records), index, "record signature does not match the journal key")
        previous = record["hash"]
    return JournalCheck(True, len(records))


def verify_journal(path: Path, *, anchor: str | None = None, key: str | None = None) -> JournalCheck:
    """Chain, then the head witness, then an optional hash stored outside the journal.

    A key (argument or ``SYNAPS_PROGRAMPLAN_JOURNAL_KEY``) also requires each
    record to carry an HMAC-SHA256 of its hash. Without a key the signature is
    not checked, so an old unsigned journal still verifies.
    """
    check = _verify_chain(path, key)
    if not check.ok:
        return check
    records = read_journal(path)
    head = records[-1]["hash"] if records else GENESIS
    try:
        witness = _read_witness(path)
    except ValueError as exc:
        return JournalCheck(False, len(records), None, str(exc))
    if records and witness is None:
        return JournalCheck(False, len(records), None, "head witness is missing")
    if witness is not None and (witness["records"] != len(records) or witness["hash"] != head):
        return JournalCheck(
            False,
            len(records),
            None,
            "head witness does not match the journal (tail removed or file replaced)",
        )
    if anchor is not None and anchor != head:
        return JournalCheck(False, len(records), None, "external anchor does not match the last record")
    return check


def seal_journal(path: Path, *, key: str | None = None) -> JournalCheck:
    """Write the head witness for a chain that does not have one yet.

    Refuses when a witness already exists and disagrees: sealing must not hide
    a truncated or replaced journal.
    """
    check = _verify_chain(path, key)
    if not check.ok:
        return check
    records = read_journal(path)
    head = records[-1]["hash"] if records else GENESIS
    witness = None
    try:
        witness = _read_witness(path)
    except ValueError as exc:
        return JournalCheck(False, len(records), None, str(exc))
    if witness is not None and (witness["records"] != len(records) or witness["hash"] != head):
        return JournalCheck(False, len(records), None, "refusing to seal over a witness that does not match")
    if witness is None:
        _write_witness(path, len(records), head)
    return check
