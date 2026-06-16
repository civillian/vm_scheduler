from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from typing import Literal, Optional
from redbeat import RedBeatSchedulerEntry
from celery.schedules import crontab
import re

from db import VmSchedule, ExecutionLog, get_session, init_db, healthcheck
from workers.celery_app import celery_app
from workers.blackouts import (
    upsert_calendar, delete_calendar, list_calendars,
)

app = FastAPI(title="VM Power Scheduler")

COLLECTOR_ENTRY = "batch:collector:tick"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ScheduleRequest(BaseModel):
    vm_id: str
    provider: Literal["aws", "azure", "vmware"]
    timezone: str = "Australia/Sydney"

    # 24x7 sentinel: both default to 0.
    # When power_on_hour == power_off_hour the VM is treated as always-on
    # and the collector skips it entirely. Prod VMs simply omit these vars.
    power_off_hour: int = 0
    power_on_hour: int = 0
    power_off_minute: int = 0
    power_on_minute: int = 0

    # Unified blackout list. "weekends" is a built-in period handled the same
    # as any named calendar. Empty list = no blackouts (always runs on schedule).
    # Default covers the most common non-prod case.
    blackout_periods: list[str] = ["weekends", "christmas-shutdown", "nat-public-holidays"]

    # AWS
    region: Optional[str] = None
    role_arn: Optional[str] = None

    # Azure
    subscription_id: Optional[str] = None
    resource_group: Optional[str] = None

    # VMware
    vcenter_host: Optional[str] = None

    @field_validator("vm_id")
    @classmethod
    def validate_vm_id(cls, v):
        if not re.match(r'^[a-zA-Z0-9_\-]+$', v):
            raise ValueError("vm_id must be alphanumeric with dashes/underscores only")
        return v

    @field_validator("power_off_hour", "power_on_hour")
    @classmethod
    def validate_hour(cls, v):
        if not 0 <= v <= 23:
            raise ValueError(f"Hour must be 0-23, got {v}")
        return v

    @field_validator("power_off_minute", "power_on_minute")
    @classmethod
    def validate_minute(cls, v):
        if not 0 <= v <= 59:
            raise ValueError(f"Minute must be 0-59, got {v}")
        return v

    @model_validator(mode="after")
    def validate_provider_fields(self):
        if self.provider == "aws":
            if not all([self.region, self.role_arn]):
                raise ValueError("AWS schedules require region and role_arn")
        if self.provider == "azure":
            if not all([self.subscription_id, self.resource_group]):
                raise ValueError("Azure schedules require subscription_id and resource_group")
        if self.provider == "vmware":
            if not self.vcenter_host:
                raise ValueError("VMware schedules require vcenter_host")
        return self

    @property
    def is_24x7(self) -> bool:
        return self.power_on_hour == self.power_off_hour == 0


class CalendarRangeEntry(BaseModel):
    type: Literal["range"] = "range"
    start: str
    end: str
    label: str

    @field_validator("start", "end")
    @classmethod
    def validate_date_format(cls, v):
        parts = v.split("-")
        if len(parts) not in (2, 3):
            raise ValueError(f"Date '{v}' must be DD-MM or DD-MM-YYYY")
        day, month = int(parts[0]), int(parts[1])
        if not (1 <= day <= 31 and 1 <= month <= 12):
            raise ValueError(f"Date '{v}' has invalid day or month")
        return v


class CalendarDayEntry(BaseModel):
    """A single date — e.g. one public holiday."""
    type: Literal["day"] = "day"
    date: str   # DD-MM-YYYY
    label: str

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v):
        parts = v.split("-")
        if len(parts) != 3:
            raise ValueError(f"Date '{v}' must be DD-MM-YYYY")
        day, month = int(parts[0]), int(parts[1])
        if not (1 <= day <= 31 and 1 <= month <= 12):
            raise ValueError(f"Date '{v}' has invalid day or month")
        return v


class CalendarUpsertRequest(BaseModel):
    entries: list[CalendarRangeEntry | CalendarDayEntry]


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _ensure_collector_running():
    try:
        RedBeatSchedulerEntry.from_key(f"redbeat:{COLLECTOR_ENTRY}", app=celery_app)
    except KeyError:
        entry = RedBeatSchedulerEntry(
            name=COLLECTOR_ENTRY,
            task="workers.batch_collector.collect_and_dispatch",
            schedule=crontab(minute="*"),
            app=celery_app,
            # If workers are down, stale ticks are dropped rather than
            # queueing up and firing back-to-back on recovery.
            options={"expires": 60},
        )
        entry.save()


@app.on_event("startup")
def startup():
    init_db()                    # create tables if they don't exist
    _ensure_collector_running()  # register the Beat tick


# ---------------------------------------------------------------------------
# Schedule endpoints
# ---------------------------------------------------------------------------

@app.post("/schedule", status_code=201)
def create_or_update_schedule(req: ScheduleRequest):
    """Upsert a VM schedule. Called by Terraform on apply."""
    # Normalise: store blackout_periods as the blackouts JSON column
    data = req.model_dump()
    data["blackouts"] = {"periods": req.blackout_periods}
    data.pop("blackout_periods", None)

    with get_session() as session:
        row = session.get(VmSchedule, req.vm_id)
        if row:
            for field, value in data.items():
                setattr(row, field, value)
        else:
            row = VmSchedule(**data)
            session.add(row)
        session.commit()

    if req.is_24x7:
        mode = "24x7 - no power operations will be scheduled"
    else:
        mode = f"off={req.power_off_hour:02d}:{req.power_off_minute:02d} on={req.power_on_hour:02d}:{req.power_on_minute:02d}"

    return {
        "status": "scheduled",
        "vm_id": req.vm_id,
        "mode": "24x7" if req.is_24x7 else "scheduled",
        "schedule": mode,
        "blackout_periods": req.blackout_periods,
    }

@app.delete("/schedule/{vm_id}", status_code=200)
def delete_schedule(vm_id: str):
    """Remove a VM schedule. Called by Terraform on destroy."""
    with get_session() as session:
        row = session.get(VmSchedule, vm_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"No schedule found for {vm_id}")
        session.delete(row)
        session.commit()
    return {"status": "deleted", "vm_id": vm_id}


@app.get("/schedule/{vm_id}")
def get_schedule(vm_id: str):
    """Current schedule and last execution result for a VM."""
    with get_session() as session:
        row = session.get(VmSchedule, vm_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"No schedule found for {vm_id}")
        return row.to_dict()


@app.get("/schedule/{vm_id}/history")
def get_vm_history(vm_id: str, limit: int = 50):
    """Execution history for a specific VM — most recent first."""
    with get_session() as session:
        rows = (
            session.query(ExecutionLog)
            .filter(ExecutionLog.vm_id == vm_id)
            .order_by(ExecutionLog.executed_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "action": r.action, "result": r.result,
                "detail": r.detail,
                "executed_at": r.executed_at.strftime("%d-%m-%Y %H:%M:%S UTC"),
            }
            for r in rows
        ]


@app.get("/schedules/summary")
def schedules_summary():
    """VM counts per provider plus recent failure summary."""
    with get_session() as session:
        rows = session.query(VmSchedule).all()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.provider] = counts.get(row.provider, 0) + 1

        recent_errors = (
            session.query(ExecutionLog)
            .filter(ExecutionLog.result == "error")
            .order_by(ExecutionLog.executed_at.desc())
            .limit(10)
            .all()
        )

    return {
        "total": len(rows),
        "by_provider": counts,
        "recent_errors": [
            {
                "vm_id": e.vm_id, "action": e.action,
                "detail": e.detail,
                "executed_at": e.executed_at.strftime("%d-%m-%Y %H:%M:%S UTC"),
            }
            for e in recent_errors
        ]
    }


# ---------------------------------------------------------------------------
# Calendar endpoints (unchanged — calendars stay in Redis)
# ---------------------------------------------------------------------------

@app.get("/calendars")
def get_calendars():
    return list_calendars()


@app.put("/calendars/{name}", status_code=200)
def put_calendar(name: str, req: CalendarUpsertRequest):
    """
    Create or replace a named blackout calendar. Full replace — send the
    complete list of entries each time (including prior years you want
    to keep pre-staged).

    Entry types:

      Single day -- e.g. one public holiday:
        { "type": "day", "date": "25-12-2026", "label": "Christmas Day" }

      Yearless date range (repeats every year, handles Dec->Jan rollover):
        { "type": "range", "start": "24-12", "end": "02-01", "label": "Christmas shutdown" }

      Specific dated range (one-off, year embedded in the dates):
        { "type": "range", "start": "24-12-2026", "end": "02-01-2027", "label": "Christmas shutdown 2026/27" }

    Entries for multiple years can be appended under the same calendar
    name -- each dated entry carries its own year, so admins can pre-stage
    several years ahead without ever touching Terraform.
    """
    entries = [e.model_dump() for e in req.entries]
    upsert_calendar(name, entries)
    return {"status": "saved", "calendar": name, "entries": entries}


@app.delete("/calendars/{name}", status_code=200)
def remove_calendar(name: str):
    if not delete_calendar(name):
        raise HTTPException(status_code=404, detail=f"Calendar '{name}' not found")
    return {"status": "deleted", "calendar": name}


# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    db_ok = healthcheck()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
    }
