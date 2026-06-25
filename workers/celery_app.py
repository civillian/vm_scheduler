"""
celery_app.py — Celery application configuration.

Redis connection is built from component env vars (Option B) so that
host, port, password, and database numbers can be configured independently
— important when sharing a Redis instance with other workloads.

  REDIS_HOST        default: redis
  REDIS_PORT        default: 6379
  REDIS_PASSWORD    default: (none)
  REDIS_BROKER_DB   default: 0   — use db 2 when sharing a Redis instance
  REDIS_RESULT_DB   default: 1   — use db 3 when sharing a Redis instance
"""

import os
from celery import Celery


def _redis_url(db_env: str, db_default: str) -> str:
    host     = os.environ.get("REDIS_HOST",     "redis")
    port     = os.environ.get("REDIS_PORT",     "6379")
    password = os.environ.get("REDIS_PASSWORD", "")
    db       = os.environ.get(db_env,           db_default)

    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


broker_url   = _redis_url("REDIS_BROKER_DB", "0")
result_url   = _redis_url("REDIS_RESULT_DB", "1")
redbeat_url  = broker_url   # redbeat shares the broker db

celery_app = Celery("vm_scheduler")

celery_app.config_from_object({
    "broker_url":    broker_url,
    "result_backend": result_url,

    # Silence Celery 6.0 forward-compatibility warning
    "broker_connection_retry_on_startup": True,

    # Redbeat scheduler
    "beat_scheduler":       "redbeat.RedBeatScheduler",
    "redbeat_redis_url":    redbeat_url,
    "redbeat_lock_timeout": 10 * 60,  # 10 minutes

    # Task reliability
    "task_acks_late":             True,
    "task_reject_on_worker_lost": True,
    "task_serializer":            "json",
    "result_serializer":          "json",
    "accept_content":             ["json"],

    "imports":   ["workers.batch_collector"],
    "timezone":  "UTC",
    "enable_utc": True,
})
