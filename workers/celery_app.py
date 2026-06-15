from celery import Celery

celery_app = Celery("vm_scheduler")

celery_app.config_from_object({
    "broker_url": "redis://redis:6379/0",
    "result_backend": "redis://redis:6379/1",

    # Redbeat scheduler
    "beat_scheduler": "redbeat.RedBeatScheduler",
    "redbeat_redis_url": "redis://redis:6379/0",
    # Prevents multiple Beat instances firing duplicate jobs
    "redbeat_lock_timeout": 10 * 60,  # 10 minutes

    # Task reliability
    "task_acks_late": True,             # Only ack after task completes
    "task_reject_on_worker_lost": True, # Re-queue if worker dies mid-task
    "task_serializer": "json",
    "result_serializer": "json",
    "accept_content": ["json"],

    # Retry defaults (overridable per task)
    "task_annotations": {
        "*": {"max_retries": 5}
    },

    "imports": ["workers.batch_collector"],
    "timezone": "UTC",
    "enable_utc": True,
})
