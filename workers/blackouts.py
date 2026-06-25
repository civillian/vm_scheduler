"""
blackouts.py — centralised blackout checking for VM power operations.

All dates expressed as DD-MM, DD-MM-YYYY, or single-day DD-MM-YYYY
(Australian format throughout).

VM blackout config is read from Postgres (vm_schedules.blackouts column).
The blackout calendar store remains in Redis — it is scheduler config,
not VM data, and does not need backup/DR beyond what's in Vault/git
(though a manual PUT replay would be needed after a Redis data loss).

Calendar entry types:
  "day"   — single date, DD-MM-YYYY. e.g. a public holiday.
  "range" — date span, either:
              - yearless DD-MM (repeats every year, handles Dec->Jan rollover)
              - specific DD-MM-YYYY (one-off, e.g. annual Christmas shutdown
                with a fixed end date that differs each year)

Entries for multiple years can coexist under the same calendar name —
each DD-MM-YYYY entry carries its own year, so admins can pre-stage
several years of dates under fixed names (e.g. "nat-public-holidays",
"christmas-shutdown") without ever needing to update Terraform.

IMPORTANT — timezone handling:
is_blackout() takes an explicit `today` (a date) computed by the caller
in the VM's local timezone. It deliberately does NOT call date.today(),
because the container's local timezone (UTC, or possibly
Australia/Sydney depending on deployment) may not match the VM's
schedule timezone, and the date can differ near local midnight.
Always pass `today` explicitly.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Optional

import os
import redis

logger = logging.getLogger(__name__)


def _redis_client() -> redis.Redis:
    host     = os.environ.get("REDIS_HOST",     "redis")
    port     = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD", "") or None
    db       = int(os.environ.get("REDIS_BROKER_DB", "0"))
    return redis.Redis(host=host, port=port, password=password,
                       db=db, decode_responses=True)

CALENDAR_KEY = "blackout:calendars"


# ---------------------------------------------------------------------------
# Calendar store — Redis only (scheduler config, not VM data)
# ---------------------------------------------------------------------------

def _load_calendars() -> dict:
    raw = _redis_client().get(CALENDAR_KEY)
    return json.loads(raw) if raw else {}


def _save_calendars(calendars: dict):
    _redis_client().set(CALENDAR_KEY, json.dumps(calendars))


def list_calendars() -> dict:
    return _load_calendars()


def upsert_calendar(name: str, entries: list[dict]):
    calendars = _load_calendars()
    calendars[name] = entries
    _save_calendars(calendars)


def delete_calendar(name: str) -> bool:
    calendars = _load_calendars()
    if name not in calendars:
        return False
    del calendars[name]
    _save_calendars(calendars)
    return True


# ---------------------------------------------------------------------------
# Date helpers (DD-MM or DD-MM-YYYY)
# ---------------------------------------------------------------------------

def _parse_date(s: str, year: int) -> date:
    """
    Parse DD-MM (yearless — uses the supplied `year`) or DD-MM-YYYY
    (specific — uses the year embedded in the string itself).
    """
    parts = s.strip().split("-")
    if len(parts) == 2:
        return date(year, int(parts[1]), int(parts[0]))
    elif len(parts) == 3:
        return date(int(parts[2]), int(parts[1]), int(parts[0]))
    raise ValueError(f"Cannot parse date '{s}' — expected DD-MM or DD-MM-YYYY")


def _in_yearless_range(start_str: str, end_str: str, today: date) -> bool:
    start = _parse_date(start_str, today.year)
    end   = _parse_date(end_str,   today.year)
    if start <= end:
        return start <= today <= end
    # Year rollover e.g. 24-12 → 02-01
    return today >= start or today <= end


def _in_specific_range(start_str: str, end_str: str, today: date) -> bool:
    # Years come from the strings themselves — supports cross-year
    # ranges like 24-12-2026 -> 02-01-2027 correctly.
    start = _parse_date(start_str, today.year)
    end   = _parse_date(end_str,   today.year)
    return start <= today <= end


def _is_day(date_str: str, today: date) -> bool:
    return _parse_date(date_str, today.year) == today


# ---------------------------------------------------------------------------
# Core blackout check
# ---------------------------------------------------------------------------

def is_blackout(vm_id: str, today: date) -> tuple[bool, str]:
    """
    Returns (True, reason) if `today` is a blackout for this VM, else (False, "").

    `today` MUST be the date in the VM's local timezone, as computed by
    the caller (see module docstring). This function performs no
    timezone conversion of its own.

    The blackouts column stores {"periods": ["weekends", "christmas-shutdown", ...]}
    "weekends" is a built-in period — all others are looked up in the calendar store.
    """
    from db import VmSchedule, get_session

    with get_session() as session:
        row = session.get(VmSchedule, vm_id)
        if not row:
            return False, ""
        blackout_config = row.blackouts or {}

    periods = blackout_config.get("periods", [])
    if not periods:
        return False, ""

    # Built-in: weekends
    if "weekends" in periods and today.weekday() >= 5:
        return True, "weekend"

    # All other periods looked up in the calendar store
    calendar_periods = [p for p in periods if p != "weekends"]
    if not calendar_periods:
        return False, ""

    all_calendars = _load_calendars()

    for cal_name in calendar_periods:
        for entry in all_calendars.get(cal_name, []):
            label = entry.get("label", cal_name)

            if entry["type"] == "day":
                if _is_day(entry["date"], today):
                    return True, f"{label} ({cal_name})"

            elif entry["type"] == "range":
                yearless = len(entry.get("start", "").split("-")) == 2
                if yearless:
                    if _in_yearless_range(entry["start"], entry["end"], today):
                        return True, f"{label} ({cal_name})"
                else:
                    if _in_specific_range(entry["start"], entry["end"], today):
                        return True, f"{label} ({cal_name})"

    return False, ""
