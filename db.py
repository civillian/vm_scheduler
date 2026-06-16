"""
db.py — SQLAlchemy models and session management.

vm_schedules  — one row per registered VM (replaces Redis vm_meta:* keys)
execution_log — audit trail of every power action attempted
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, JSON, String, Text,
    create_engine, text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://scheduler:scheduler@postgres:5432/scheduler"
)

engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,   # cheap liveness check before each checkout
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class VmSchedule(Base):
    """
    One row per registered VM. Written by the API on Terraform apply,
    deleted on Terraform destroy. Read every minute by the batch collector.
    """
    __tablename__ = "vm_schedules"

    vm_id            = Column(String(255), primary_key=True)
    provider         = Column(String(32),  nullable=False)
    timezone         = Column(String(64),  nullable=False, default="Australia/Sydney")

    power_off_hour   = Column(Integer, nullable=False)
    power_off_minute = Column(Integer, nullable=False, default=0)
    power_on_hour    = Column(Integer, nullable=False)
    power_on_minute  = Column(Integer, nullable=False, default=0)
    # Blackout periods: {"periods": ["weekends", "christmas-shutdown", ...]}
    # "weekends" is built-in; all others are named calendar lookups.
    # Empty list = no blackouts. Both hours = 0 means 24x7 mode.
    blackouts        = Column(JSON, nullable=False, default=dict)

    # AWS
    region           = Column(String(64))
    role_arn         = Column(String(255))

    # Azure
    subscription_id  = Column(String(64))
    resource_group   = Column(String(128))

    # VMware
    vcenter_host     = Column(String(255))

    # Audit — updated by the batch collector after each execution
    last_power_off_at     = Column(DateTime(timezone=True))
    last_power_on_at      = Column(DateTime(timezone=True))
    last_power_off_result = Column(String(64))   # e.g. "success", "suppressed", "error"
    last_power_on_result  = Column(String(64))

    created_at       = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at       = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                              onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class ExecutionLog(Base):
    """
    Append-only record of every power action attempted.
    Provides the audit trail deferred from the original POC design.
    """
    __tablename__ = "execution_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    vm_id       = Column(String(255), nullable=False, index=True)
    provider    = Column(String(32),  nullable=False)
    action      = Column(String(16),  nullable=False)   # "on" or "off"
    result      = Column(String(64),  nullable=False)   # "success", "suppressed", "error"
    detail      = Column(Text)                          # error message or suppression reason
    executed_at = Column(DateTime(timezone=True),
                         default=lambda: datetime.now(timezone.utc),
                         nullable=False, index=True)


def get_session() -> Session:
    return SessionLocal()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)


def healthcheck() -> bool:
    """Verify database connectivity. Used by /healthz."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
