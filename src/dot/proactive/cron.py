"""Five-field crontab expressions as APScheduler triggers.

APScheduler 3's ``CronTrigger.from_crontab`` passes the day-of-week field
through unchanged, and APScheduler numbers Monday 0, so crontab's ``1-5``
would run Tuesday to Saturday. Weekday numbers are rewritten as names first.
"""

from __future__ import annotations

import re
from datetime import tzinfo

from apscheduler.triggers.cron import CronTrigger

_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NUMBER = re.compile(r"\d+")


def cron_trigger(expression: str, timezone: tzinfo) -> CronTrigger:
    """Raises ValueError for anything that is not a valid five-field crontab."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"cron {expression!r} must have five fields")
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_named_days(day_of_week, expression),
        timezone=timezone,
    )


def _named_days(field: str, expression: str) -> str:
    # A step counts from APScheduler's Monday, not crontab's Sunday.
    if "/" in field:
        raise ValueError(f"cron {expression!r}: steps are not supported in the day-of-week field")

    def name(match: re.Match[str]) -> str:
        number = int(match.group())
        if number >= len(_DAYS):
            raise ValueError(f"cron {expression!r} has day of week {number}")
        return _DAYS[number]

    named = _DAY_NUMBER.sub(name, field)
    # Crontab allows ``5-7``; APScheduler cannot range across Sunday.
    for start, end in re.findall(r"([a-z]{3})-([a-z]{3})", named):
        if _DAYS.index(start) > _DAYS.index(end) and end == "sun":
            named = named.replace(f"{start}-{end}", f"{start}-sat,sun")
    return named
