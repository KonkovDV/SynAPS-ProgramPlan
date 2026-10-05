from __future__ import annotations

from datetime import date

import pytest

from synaps_programplan.calendar import DayCounter, WorkCalendar, WorkdayAxis, is_provisional
from synaps_programplan.model import Calendar, CalendarBase

RU = WorkCalendar.from_model(Calendar(id="ru", base=CalendarBase.RU_PRODUCTION))


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_decreed_years_have_247_working_days(year: int) -> None:
    assert RU.count_workdays(date(year, 1, 1), date(year, 12, 31)) == 247
    assert not is_provisional(year)


def test_decreed_holidays_and_transfers() -> None:
    assert not RU.is_workday(date(2026, 1, 1))
    assert not RU.is_workday(date(2026, 1, 9))  # 3 Jan 2026 moved to 9 Jan
    assert not RU.is_workday(date(2026, 11, 4))
    assert RU.is_workday(date(2025, 11, 1))  # working Saturday
    assert RU.is_workday(date(2026, 10, 5))


def test_years_without_decree_are_provisional() -> None:
    assert is_provisional(2028)
    assert not RU.is_workday(date(2028, 1, 1))
    assert not RU.is_workday(date(2028, 5, 9))


def test_axis_semantics() -> None:
    axis = WorkdayAxis.build(RU, date(2026, 10, 9), date(2026, 12, 31))  # Friday
    assert axis.days[0] == date(2026, 10, 9)
    assert axis.days[1] == date(2026, 10, 12)  # weekend skipped
    assert axis.index_on_or_after(date(2026, 10, 10)) == 1
    assert axis.boundary_after(date(2026, 10, 9)) == 1
    assert axis.finish_date(0, 2) == date(2026, 10, 12)  # two working days
    assert axis.event_date(1) == date(2026, 10, 9)  # milestone at boundary 1


def test_day_counter_roundtrip() -> None:
    counter = DayCounter(RU)
    ordinal = counter.ordinal_on_or_after(date(2026, 11, 4))  # holiday -> next working day
    assert counter.date_of(ordinal) == date(2026, 11, 5)
