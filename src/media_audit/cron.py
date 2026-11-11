"""Minimal 5-field cron expressions for scheduled scans.

Supports ``*``, numbers, ranges (``1-5``), steps (``*/15``, ``0-30/10``), lists
(``1,15``), month and weekday names (``jan``, ``mon-fri``), weekday 7 = Sunday, and the
shortcuts ``@hourly``, ``@daily``/``@midnight``, ``@weekly``, ``@monthly``, ``@yearly``.
As in standard cron, when both day-of-month and day-of-week are restricted a day
matches if *either* does.

Times are naive local wall-clock times (the container's ``TZ``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


class CronError(ValueError):
    pass


_SHORTCUTS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, start=1)}
_DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
# (name, low, high, names)
_FIELDS: tuple[tuple[str, int, int, dict[str, int]], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
)


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    days_restricted: bool
    weekdays_restricted: bool

    def _day_matches(self, dt: datetime) -> bool:
        dom = dt.day in self.days
        dow = (dt.isoweekday() % 7) in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return dom or dow
        return dom and dow

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._day_matches(dt)
        )

    def next_after(self, after: datetime) -> datetime:
        """First matching minute strictly after ``after``."""
        dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = dt + timedelta(days=366 * 5)  # e.g. "0 0 29 2 *" waits up to 4 years
        while dt < limit:
            if dt.month not in self.months:
                dt = (dt.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            elif not self._day_matches(dt):
                dt = dt.replace(hour=0, minute=0) + timedelta(days=1)
            elif dt.hour not in self.hours:
                dt = dt.replace(minute=0) + timedelta(hours=1)
            elif dt.minute not in self.minutes:
                dt += timedelta(minutes=1)
            else:
                return dt
        raise CronError(f"{self.expr!r} never matches")


def _value(token: str, low: int, high: int, names: dict[str, int], field: str) -> int:
    t = token.lower()
    if t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(f"bad {field} value {token!r}")
    n = int(t)
    if not low <= n <= high:
        raise CronError(f"{field} value {n} out of range {low}-{high}")
    return n


def _field(spec: str, low: int, high: int, names: dict[str, int], field: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"bad {field} step {step_s!r}")
            step = int(step_s)
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start, end = (_value(a, low, high, names, field), _value(b, low, high, names, field))
            if start > end:
                raise CronError(f"bad {field} range {rng!r}")
        else:
            start = _value(rng, low, high, names, field)
            end = high if step_s else start
        out.update(range(start, end + 1, step))
    return out


def parse_cron(expr: str) -> Cron:
    text = _SHORTCUTS.get(expr.strip().lower(), expr.strip())
    parts = text.split()
    if len(parts) != 5:
        raise CronError(
            f"cron expression {expr!r} needs 5 fields (minute hour day month weekday), "
            "e.g. '0 3 * * *' for 03:00 daily"
        )
    sets = [
        _field(p, lo, hi, names, name)
        for p, (name, lo, hi, names) in zip(parts, _FIELDS, strict=True)
    ]
    weekdays = {0 if d == 7 else d for d in sets[4]}
    cron = Cron(
        expr=expr.strip(),
        minutes=frozenset(sets[0]),
        hours=frozenset(sets[1]),
        days=frozenset(sets[2]),
        months=frozenset(sets[3]),
        weekdays=frozenset(weekdays),
        days_restricted=parts[2] != "*",
        weekdays_restricted=parts[4] != "*",
    )
    cron.next_after(datetime(2000, 1, 1))  # reject impossible dates like "0 0 31 2 *"
    return cron
