import json
import logging
import logging.config
import re
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from redbeat import RedBeatSchedulerEntry
from celery.schedules import crontab
from typing import Optional

from db import VmSchedule, ExecutionLog, get_session, init_db, healthcheck
from workers.celery_app import celery_app
from workers.blackouts import (
    upsert_calendar, delete_calendar, list_calendars,
)
from workers.vault import VaultConfigError

# ---------------------------------------------------------------------------
# Logging — add timestamps to uvicorn access logs
# ---------------------------------------------------------------------------

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(asctime)s %(levelprefix)s %(message)s",
            "datefmt": "%d-%m-%Y %H:%M:%S",
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": "%d-%m-%Y %H:%M:%S",
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

logging.config.dictConfig(LOGGING_CONFIG)

app = FastAPI(title="VM Power Scheduler")

COLLECTOR_ENTRY = "batch:collector:tick"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ScheduleRequest(BaseModel):
    vm_id:        str
    display_name: Optional[str] = None    # from module.naming-service.generated_vm_name
    provider:     str                      # "aws" | "azure" | "vmware"
    timezone:     str = "Australia/Sydney"

    # 24x7 sentinel: both default to 0. When power_on_hour == power_off_hour == 0
    # the VM is treated as always-on and the collector skips it entirely.
    power_off_hour:   int = 0
    power_on_hour:    int = 0
    power_off_minute: int = 0
    power_on_minute:  int = 0

    # Unified blackout list. "weekends" is built-in; others are calendar names.
    blackout_periods: list[str] = ["weekends"]

    # All provider-specific fields in one flexible dict.
    # AWS:    {"role_arn": "...", "region": "ap-southeast-2"}
    # Azure:  {"tenant_id": "...", "subscription_id": "...",
    #          "resource_group": "...", "vault_role": "workspace-name"}
    # VMware: {"vcenter_host": "vcenter.internal.example.com"}
    provider_config: dict = {}

    @field_validator("vm_id")
    @classmethod
    def validate_vm_id(cls, v):
        if not re.match(r'^[a-zA-Z0-9_\-]+$', v):
            raise ValueError(
                "vm_id must be alphanumeric with dashes/underscores only"
            )
        return v

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v):
        if v not in ("aws", "azure", "vmware"):
            raise ValueError("provider must be one of: aws, azure, vmware")
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
    def validate_provider_config(self):
        cfg = self.provider_config
        if self.provider == "aws":
            if not cfg.get("role_arn"):
                raise ValueError(
                    "AWS provider_config requires 'role_arn'"
                )
            if not cfg.get("region"):
                raise ValueError(
                    "AWS provider_config requires 'region'"
                )
        if self.provider == "azure":
            for field in ("tenant_id", "subscription_id",
                          "resource_group", "vault_role"):
                if not cfg.get(field):
                    raise ValueError(
                        f"Azure provider_config requires '{field}'"
                    )
        if self.provider == "vmware":
            if not cfg.get("vcenter_host"):
                raise ValueError(
                    "VMware provider_config requires 'vcenter_host'"
                )
        return self

    @property
    def is_24x7(self) -> bool:
        return self.power_on_hour == self.power_off_hour == 0


class CalendarRangeEntry(BaseModel):
    type:  str = "range"
    start: str
    end:   str
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
    type:  str = "day"
    date:  str   # DD-MM-YYYY
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
        RedBeatSchedulerEntry.from_key(
            f"redbeat:{COLLECTOR_ENTRY}", app=celery_app
        )
    except KeyError:
        entry = RedBeatSchedulerEntry(
            name=COLLECTOR_ENTRY,
            task="workers.batch_collector.collect_and_dispatch",
            schedule=crontab(minute="*"),
            app=celery_app,
            options={"expires": 60},
        )
        entry.save()


@app.on_event("startup")
def startup():
    init_db()
    _ensure_collector_running()


# ---------------------------------------------------------------------------
# Schedule endpoints
# ---------------------------------------------------------------------------

@app.post("/schedule", status_code=201)
def create_or_update_schedule(req: ScheduleRequest):
    """Upsert a VM schedule. Called by Terraform on apply."""
    data = req.model_dump()
    data["blackouts"] = {"periods": req.blackout_periods}
    data.pop("blackout_periods", None)
    data.pop("is_24x7", None)

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
        mode = "24x7 — no power operations will be scheduled"
    else:
        mode = (
            f"off={req.power_off_hour:02d}:{req.power_off_minute:02d} "
            f"on={req.power_on_hour:02d}:{req.power_on_minute:02d}"
        )

    return {
        "status":          "scheduled",
        "vm_id":           req.vm_id,
        "display_name":    req.display_name,
        "mode":            "24x7" if req.is_24x7 else "scheduled",
        "schedule":        mode,
        "blackout_periods": req.blackout_periods,
    }


@app.delete("/schedule/{vm_id}", status_code=200)
def delete_schedule(vm_id: str):
    """Remove a VM schedule. Called by Terraform on destroy."""
    with get_session() as session:
        row = session.get(VmSchedule, vm_id)
        if not row:
            raise HTTPException(
                status_code=404, detail=f"No schedule found for {vm_id}"
            )
        session.delete(row)
        session.commit()
    return {"status": "deleted", "vm_id": vm_id}


@app.get("/schedule/{vm_id}")
def get_schedule(vm_id: str):
    """Current schedule and last execution result for a VM."""
    with get_session() as session:
        row = session.get(VmSchedule, vm_id)
        if not row:
            raise HTTPException(
                status_code=404, detail=f"No schedule found for {vm_id}"
            )
        return row.to_dict()


@app.get("/schedule/{vm_id}/history")
def get_vm_history(vm_id: str, limit: int = 50):
    """Execution history for a VM — most recent first."""
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
                "action":       r.action,
                "result":       r.result,
                "detail":       r.detail,
                "display_name": r.display_name,
                "executed_at":  r.executed_at.strftime("%d-%m-%Y %H:%M:%S UTC"),
            }
            for r in rows
        ]


@app.get("/schedules/summary")
def schedules_summary():
    """VM counts per provider plus recent failures."""
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
        "total":       len(rows),
        "by_provider": counts,
        "recent_errors": [
            {
                "vm_id":        e.vm_id,
                "display_name": e.display_name,
                "action":       e.action,
                "detail":       e.detail,
                "executed_at":  e.executed_at.strftime("%d-%m-%Y %H:%M:%S UTC"),
            }
            for e in recent_errors
        ]
    }


# ---------------------------------------------------------------------------
# Calendar endpoints
# ---------------------------------------------------------------------------

@app.get("/calendars")
def get_calendars():
    return list_calendars()


@app.put("/calendars/{name}", status_code=200)
def put_calendar(name: str, req: CalendarUpsertRequest):
    """
    Create or replace a named blackout calendar. Full replace — send the
    complete list of entries each time including prior years.

    Single day:
      {"type": "day",   "date": "25-12-2026", "label": "Christmas Day"}

    Yearless range (repeats annually, handles Dec->Jan rollover):
      {"type": "range", "start": "24-12", "end": "02-01",
       "label": "Christmas shutdown"}

    Specific dated range (one-off):
      {"type": "range", "start": "24-12-2026", "end": "02-01-2027",
       "label": "Christmas shutdown 2026/27"}
    """
    entries = [e.model_dump() for e in req.entries]
    upsert_calendar(name, entries)
    return {"status": "saved", "calendar": name, "entries": entries}


@app.delete("/calendars/{name}", status_code=200)
def remove_calendar(name: str):
    if not delete_calendar(name):
        raise HTTPException(
            status_code=404, detail=f"Calendar '{name}' not found"
        )
    return {"status": "deleted", "calendar": name}


# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    db_ok = healthcheck()
    return {
        "status":   "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
    }


@app.get("/")
def root():
    return {"service": "vm-scheduler", "status": "ok"}
