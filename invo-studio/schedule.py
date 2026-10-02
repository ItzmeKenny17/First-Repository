"""Phoenix posting slots with a hard two-hour interval.

An activation date starts the ramp. A 'day' runs from 9 AM to 9 AM
Phoenix time, allowing a full 12-slot cycle without adjacent uploads.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Phoenix")
RAMP = (5, 7, 10, 11, 12)


def slot(now: datetime, start: date) -> tuple[int, int, bool]:
    local = now.astimezone(ZONE)
    hour = local.hour
    cycle_date = local.date() if hour >= 9 else local.date() - timedelta(days=1)
    day_index = (cycle_date - start).days
    slot_index = ((hour - 9) % 24) // 2
    on_slot = hour % 2 == 1 and local.minute >= 10 and local.minute < 45
    allowed = day_index >= 0 and on_slot and slot_index < RAMP[min(day_index, len(RAMP) - 1)]
    return day_index, slot_index, allowed


def next_eligible(now: datetime, start: date, last_post: datetime | None = None) -> datetime | None:
    """First configured Phoenix slot after now and at least 2h after a live post."""
    local = now.astimezone(ZONE)
    lower_bound = None if last_post is None else last_post.astimezone(ZONE) + timedelta(hours=2)
    for offset in range(0, 10):
        cycle = local.date() + timedelta(days=offset - 1)
        day = (cycle - start).days
        if day < 0:
            continue
        for index in range(RAMP[min(day, len(RAMP) - 1)]):
            candidate = datetime(cycle.year, cycle.month, cycle.day, 9, 13, tzinfo=ZONE) + timedelta(hours=2 * index)
            if candidate > local and (lower_bound is None or candidate >= lower_bound):
                return candidate
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    day, index, allowed = slot(datetime.now(ZONE), args.start_date)
    print(f"day={day + 1} slot={index + 1} allowed={str(allowed).lower()}")
    raise SystemExit(0 if allowed else 3)
