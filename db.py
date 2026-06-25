"""
db.py — SQLAlchemy models and session management.

vm_schedules  — one row per registered VM
execution_log — append-only audit trail of every power action attempted

provider_config is a JSONB column storing all provider-specific fields:

  AWS:    {"role_arn": "...", "region": "ap-southeast-2"}
  Azure:  {"tenant_id": "...", "subscription_id": "...",
           "resource_group": "...", "vault_role": "workspace-name"}
  VMware: {"vcenter_host": "vcenter.internal.example.com"}

This keeps the schema provider-agnostic and allows new fields to be
added without schema migrations.
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
    pool_pre_ping=True,
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
    display_name     = Column(String(255), nullable=True)   # from naming service
    provider         = Column(String(32),  nullable=False)
    timezone         = Column(String(64),  nullable=False, default="Australia/Sydney")

    power_off_hour   = Column(Integer, nullable=False, default=0)
    power_off_minute = Column(Integer, nullable=False, default=0)
    power_on_hour    = Column(Integer, nullable=False, default=0)
    power_on_minute  = Column(Integer, nullable=False, default=0)

    # Blackout periods: {"periods": ["weekends", "christmas-shutdown", ...]}
    blackouts        = Column(JSON, nullable=False, default=dict)

    # All provider-specific fields — see module docstring for shapes per provider
    provider_config  = Column(JSON, nullable=False, default=dict)

    # Audit — updated by batch collector after each execution
    last_power_off_at     = Column(DateTime(timezone=True))
    last_power_on_at      = Column(DateTime(timezone=True))
    last_power_off_result = Column(String(64))
    last_power_on_result  = Column(String(64))

    created_at       = Column(DateTime(timezone=True),
                              default=lambda: datetime.now(timezone.utc))
    updated_at       = Column(DateTime(timezone=True),
                              default=lambda: datetime.now(timezone.utc),
                              onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class ExecutionLog(Base):
    """Append-only record of every power action attempted."""
    __tablename__ = "execution_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    vm_id       = Column(String(255), nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    provider    = Column(String(32),  nullable=False)
    action      = Column(String(16),  nullable=False)   # "on" or "off"
    result      = Column(String(64),  nullable=False)   # "success", "suppressed", "error", "expired"
    detail      = Column(Text)
    executed_at = Column(DateTime(timezone=True),
                         default=lambda: datetime.now(timezone.utc),
                         nullable=False, index=True)


def get_session() -> Session:
    return SessionLocal()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    Base.metadata.create_all(bind=engine)


def healthcheck() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
