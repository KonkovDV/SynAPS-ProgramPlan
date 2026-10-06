"""Run one solve in a child process and stop it when it exceeds a memory cap.

The cap is the working set of the child, in megabytes. It is off unless a
caller passes ``memory_mb``. The compute service turns it on with
``SYNAPS_PROGRAMPLAN_MEMORY_MB``.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import time
from dataclasses import asdict
from pathlib import Path

from synaps_programplan.model import OKRProgram
from synaps_programplan.planner import SolveConfig, plan
from synaps_programplan.result import PlanResult


def process_rss_mb(pid: int) -> float | None:
    """Working set of a process, or None when it cannot be read."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_rss_mb(pid)
    status = f"/proc/{pid}/status"
    try:
        text = Path(status).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return None


def plan_isolated(
    program: OKRProgram,
    config: SolveConfig,
    *,
    memory_mb: int,
    timeout_s: int | None = None,
) -> PlanResult:
    """Solve in a spawned process. Raise if it runs out of memory or time."""
    if memory_mb < 1:
        raise ValueError("memory_mb must be at least 1")
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[str] = context.Queue()
    process = context.Process(
        target=_solve_worker,
        args=(program.model_dump_json(), asdict(config), queue),
    )
    process.start()
    deadline = time.monotonic() + (timeout_s if timeout_s is not None else config.time_limit_s + 30)
    reason = ""
    while process.is_alive():
        if time.monotonic() > deadline:
            reason = "timeout"
            process.terminate()
            break
        rss = process_rss_mb(process.pid or 0)
        if rss is not None and rss > memory_mb:
            reason = "memory"
            process.terminate()
            break
        time.sleep(0.05)
    process.join(10)
    if reason == "memory":
        raise MemoryError(f"solver process exceeded {memory_mb} MiB")
    if reason == "timeout":
        raise TimeoutError("solver process exceeded its time limit")
    if process.exitcode not in (0, None):
        raise RuntimeError(f"solver process exited with code {process.exitcode}")
    try:
        raw = queue.get_nowait()
    except Exception as exc:
        raise RuntimeError("solver process ended without a plan") from exc
    return PlanResult.model_validate_json(raw)


def _solve_worker(program_json: str, config: dict[str, object], queue: multiprocessing.Queue[str]) -> None:
    program = OKRProgram.model_validate_json(program_json)
    result = plan(program, SolveConfig(**config))  # type: ignore[arg-type]
    queue.put(result.model_dump_json())


def _windows_rss_mb(pid: int) -> float | None:
    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    query = 0x0400 | 0x0010
    handle = ctypes.windll.kernel32.OpenProcess(query, False, pid)
    if not handle:
        return None
    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    try:
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    if not ok:
        return None
    return float(counters.WorkingSetSize) / (1024 * 1024)
