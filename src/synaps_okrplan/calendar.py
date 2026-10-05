"""Working-day calendars and the virtual working-day axis.

The axis is the key compilation trick: SynAPS works in integer minutes, and
OKRPlan maps ONE kernel minute to ONE working day of the program calendar.
Closed days vanish from the axis, so a multi-day task never "straddles" a
weekend and CP-SAT domains stay small (5 years ~ 1 250 values).

Russian production calendar
---------------------------
Statutory holidays: Labour Code art. 112. Weekend transfers are fixed per year
by Government decree; years with a published decree are encoded exactly:

* 2025 - decree No 1335 of 04.10.2024 (247 working days)
* 2026 - decree No 1466 of 24.09.2025 (247 working days)
* 2027 - decree No 1187 of 17.09.2026 (247 working days)

Other years use the art. 112 part 2 rule only (a weekend that coincides with a
holiday outside 1-8 January moves to the next working day) and are reported as
``provisional`` until a decree is added.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache

from synaps_okrplan.model import Calendar, CalendarBase

_STATUTORY = [(1, d) for d in range(1, 9)] + [(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)]

# Year -> (extra days off on weekdays, working weekend days), as decreed.
_DECREED: dict[int, tuple[frozenset[date], frozenset[date]]] = {
    2025: (
        frozenset(
            date(2025, m, d)
            for m, d in [
                (1, 1),
                (1, 2),
                (1, 3),
                (1, 6),
                (1, 7),
                (1, 8),
                (5, 1),
                (5, 2),
                (5, 8),
                (5, 9),
                (6, 12),
                (6, 13),
                (11, 3),
                (11, 4),
                (12, 31),
            ]
        ),
        frozenset({date(2025, 11, 1)}),
    ),
    2026: (
        frozenset(
            date(2026, m, d)
            for m, d in [
                (1, 1),
                (1, 2),
                (1, 5),
                (1, 6),
                (1, 7),
                (1, 8),
                (1, 9),
                (2, 23),
                (3, 9),
                (5, 1),
                (5, 11),
                (6, 12),
                (11, 4),
                (12, 31),
            ]
        ),
        frozenset(),
    ),
    2027: (
        frozenset(
            date(2027, m, d)
            for m, d in [
                (1, 1),
                (1, 4),
                (1, 5),
                (1, 6),
                (1, 7),
                (1, 8),
                (2, 22),
                (2, 23),
                (3, 8),
                (5, 3),
                (5, 10),
                (6, 14),
                (11, 4),
                (11, 5),
                (12, 31),
            ]
        ),
        frozenset({date(2027, 2, 20)}),
    ),
}


def decreed_years() -> list[int]:
    return sorted(_DECREED)


@lru_cache(maxsize=64)
def _ru_year(year: int) -> tuple[frozenset[date], frozenset[date]]:
    """(days off on weekdays, working weekend days) for one year."""
    if year in _DECREED:
        return _DECREED[year]
    off: set[date] = set()
    holidays = sorted(date(year, m, d) for m, d in _STATUTORY)
    holiday_set = set(holidays)
    for day in holidays:
        if day.weekday() < 5:
            off.add(day)
    for day in holidays:
        if day.weekday() >= 5 and not (day.month == 1 and day.day <= 8):
            moved = day + timedelta(days=1)
            while moved.weekday() >= 5 or moved in holiday_set or moved in off:
                moved += timedelta(days=1)
            off.add(moved)
    return frozenset(off), frozenset()


def is_provisional(year: int) -> bool:
    return year not in _DECREED


@dataclass(frozen=True)
class WorkCalendar:
    """Day-level working calendar (``is_workday``) built from a ``Calendar`` row."""

    base: CalendarBase
    extra_holidays: frozenset[date] = field(default_factory=frozenset)
    extra_workdays: frozenset[date] = field(default_factory=frozenset)

    @classmethod
    def from_model(cls, calendar: Calendar) -> WorkCalendar:
        return cls(
            base=calendar.base,
            extra_holidays=frozenset(calendar.extra_holidays),
            extra_workdays=frozenset(calendar.extra_workdays),
        )

    def is_workday(self, day: date) -> bool:
        if day in self.extra_holidays:
            return False
        if day in self.extra_workdays:
            return True
        if self.base is CalendarBase.SEVEN_DAY:
            return True
        weekday = day.weekday() < 5
        if self.base is CalendarBase.FIVE_DAY:
            return weekday
        off, working_weekends = _ru_year(day.year)
        if day in working_weekends:
            return True
        return weekday and day not in off

    def workdays(self, start: date, end: date) -> list[date]:
        """Working days in the inclusive range ``[start, end]``."""
        out: list[date] = []
        day = start
        while day <= end:
            if self.is_workday(day):
                out.append(day)
            day += timedelta(days=1)
        return out

    def count_workdays(self, start: date, end: date) -> int:
        return len(self.workdays(start, end))


@dataclass(frozen=True)
class WorkdayAxis:
    """Ordered working days; index ``k`` is the k-th working day of the plan.

    A task with start index ``k`` and duration ``d`` occupies days ``k..k+d-1``
    and ENDS at boundary ``k+d``. Boundary ``t`` closes working day ``t-1``.
    """

    days: tuple[date, ...]

    @classmethod
    def build(cls, calendar: WorkCalendar, start: date, end: date) -> WorkdayAxis:
        days = tuple(calendar.workdays(start, end))
        if not days:
            raise ValueError(f"no working days between {start} and {end}")
        return cls(days=days)

    def __len__(self) -> int:
        return len(self.days)

    def index_on_or_after(self, day: date) -> int:
        """First working-day index whose date is >= ``day`` (len when past the end)."""
        return bisect_left(self.days, day)

    def boundary_after(self, day: date) -> int:
        """Boundary closing the last working day <= ``day`` (0 when before the axis)."""
        return bisect_right(self.days, day)

    def start_date(self, index: int) -> date:
        return self.days[min(max(index, 0), len(self.days) - 1)]

    def finish_date(self, start_index: int, end_boundary: int) -> date:
        """Finish date: the last occupied day; a milestone reports the day it closes."""
        if end_boundary > start_index:
            return self.days[end_boundary - 1]
        if end_boundary <= 0:
            return self.days[0]
        return self.days[min(end_boundary, len(self.days)) - 1]

    def event_date(self, boundary: int) -> date:
        return self.finish_date(boundary, boundary)


@dataclass
class DayCounter:
    """Working-day ordinals on an arbitrary date range (used by the checker)."""

    calendar: WorkCalendar
    _cache: dict[date, int] = field(default_factory=dict)
    _origin: date = date(2000, 1, 3)
    _days: list[date] = field(default_factory=list)

    def _extend_to(self, day: date) -> None:
        if day < self._origin:
            raise ValueError(f"dates before {self._origin} are not supported")
        cursor = self._days[-1] + timedelta(days=1) if self._days else self._origin
        while cursor <= day:
            if self.calendar.is_workday(cursor):
                self._days.append(cursor)
            cursor += timedelta(days=1)

    def ordinal_on_or_after(self, day: date) -> int:
        self._extend_to(day + timedelta(days=14))
        return bisect_left(self._days, day)

    def boundary_after(self, day: date) -> int:
        self._extend_to(day + timedelta(days=14))
        return bisect_right(self._days, day)

    def date_of(self, ordinal: int) -> date:
        while len(self._days) <= ordinal:
            last = self._days[-1] if self._days else self._origin
            self._extend_to(last + timedelta(days=60))
        return self._days[ordinal]

    def is_workday(self, day: date) -> bool:
        return self.calendar.is_workday(day)
