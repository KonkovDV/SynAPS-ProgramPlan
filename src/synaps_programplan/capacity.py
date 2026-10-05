"""Weighted load profiles over working-day ordinals.

Occupancy is half-open ``[start, end)`` in working-day ordinals; ``units`` is
integer demand. A difference array gives the exact per-day load, so the peak
and every overloaded run are exact (no sampling).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Load:
    start: int
    end: int
    units: int
    ref: str


@dataclass(frozen=True, slots=True)
class Overload:
    start: int
    end: int
    peak_units: int
    available: int
    refs: tuple[str, ...]


def load_profile(rows: list[Load], lo: int, hi: int) -> list[int]:
    """Units in use on each ordinal of ``[lo, hi)``."""
    size = max(0, hi - lo)
    diff = [0] * (size + 1)
    for row in rows:
        start = max(row.start, lo)
        end = min(row.end, hi)
        if start >= end:
            continue
        diff[start - lo] += row.units
        diff[end - lo] -= row.units
    profile: list[int] = []
    running = 0
    for index in range(size):
        running += diff[index]
        profile.append(running)
    return profile


def overloads(rows: list[Load], available: list[int], lo: int) -> list[Overload]:
    """Maximal runs where load exceeds availability, with the tasks involved."""
    hi = lo + len(available)
    profile = load_profile(rows, lo, hi)
    out: list[Overload] = []
    index = 0
    while index < len(profile):
        if profile[index] <= available[index]:
            index += 1
            continue
        end = index
        peak = 0
        cap = available[index]
        while end < len(profile) and profile[end] > available[end]:
            peak = max(peak, profile[end])
            cap = min(cap, available[end])
            end += 1
        refs = tuple(sorted({row.ref for row in rows if row.start < lo + end and row.end > lo + index}))
        out.append(Overload(start=lo + index, end=lo + end, peak_units=peak, available=cap, refs=refs))
        index = end
    return out
