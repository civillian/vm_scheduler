"""
batch_collector.py — groups VMs due for a power action and dispatches
one API call per account/subscription rather than one per VM.

Schedule metadata is read from Postgres (vm_schedules table).
Redis is used only as the Celery broker and redbeat schedule store.
Execution results are written back to Postgres (execution_log table).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from db import ExecutionLog, VmSchedule, get_session
from workers.celery_app import celery_app
from workers.blackouts import is_blackout

logger = logging.getLogger(__name__)

# How long a dispatched batch task is allowed to sit in the queue before
# Celery discards it unexecuted. If all workers are down for longer than
# this, a missed power on/off simply doesn't happen for that cycle —
# the VM stays in its current state until the next scheduled cycle.
# This avoids a backlog of stale power operations firing late on recovery.
TASK_EXPIRY_SECONDS = 300   # 5 minutes

# The collector tick itself also expires quickly — if Beat queues several
# ticks while workers are down, stale ticks are dropped rather than all
# firing back-to-back on recovery and re-evaluating against a stale "now".
COLLECTOR_EXPIRY_SECONDS = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_vm_schedules() -> list[dict]:
    """
    Fetch all VM schedules from Postgres as plain dicts.
    Single SELECT, no filtering — at 5,000 rows this is <5ms.
    Option B (timezone-pushdown WHERE clause) available if fleet grows to 50k+.
    """
    with get_session() as session:
        rows = session.query(VmSchedule).all()
        return [row.to_dict() for row in rows]


def _is_24x7(meta: dict) -> bool:
    """24x7 sentinel — both hours are 0, VM should never be power-cycled."""
    return meta.get("power_on_hour", 0) == 0 and meta.get("power_off_hour", 0) == 0


def _local_now(meta: dict, now_utc: datetime) -> datetime:
    """
    Convert "now" (UTC) into the VM's own timezone.

    Used for both the due-time check and the blackout date check, so the
    two never disagree about what day or hour it currently is for this VM
    — regardless of the container's own local timezone (which may be UTC,
    or Australia/Sydney, depending on deployment).
    """
    tz = ZoneInfo(meta.get("timezone") or "UTC")
    return now_utc.astimezone(tz)


def _is_due(meta: dict, action: str, now_local: datetime) -> bool:
    """
    Return True if this VM's scheduled action is due in the current minute,
    evaluated in the VM's own timezone (now_local — see _local_now).
    Weekday/weekend filtering is handled downstream by is_blackout().
    """
    if _is_24x7(meta):
        return False

    scheduled_hour   = meta.get(f"power_{action}_hour", 0)
    scheduled_minute = meta.get(f"power_{action}_minute", 0)

    return (
        now_local.hour   == scheduled_hour and
        now_local.minute == scheduled_minute
    )


def _group_key(meta: dict) -> tuple:
    """
    Determines which VMs can be dispatched together in a single batch.

    AWS:   grouped by (role_arn, region). Each role is scoped to a single
           workload/workspace, so this naturally limits batch blast radius
           to the instances that role actually has permission over.
    Azure: grouped by subscription_id — rate limiting is per-subscription.
    VMware: grouped by vcenter_host — no batch API, but groups concurrent
           dispatch by target vCenter.
    """
    provider = meta["provider"]
    if provider == "aws":
        return (provider, meta.get("role_arn", ""), meta.get("region", ""))
    if provider == "azure":
        return (provider, meta.get("subscription_id", ""))
    return (provider, meta.get("vcenter_host", ""))


def _write_log(vm_id: str, provider: str, action: str, result: str, detail: str | None = None):
    """
    Append one row to execution_log and update the summary columns on vm_schedules.
    Called by each batch task after execution.
    """
    now = datetime.now(timezone.utc)
    with get_session() as session:
        session.add(ExecutionLog(
            vm_id=vm_id, provider=provider,
            action=action, result=result,
            detail=detail, executed_at=now,
        ))

        row = session.get(VmSchedule, vm_id)
        if row:
            if action == "off":
                row.last_power_off_at     = now
                row.last_power_off_result = result
            else:
                row.last_power_on_at     = now
                row.last_power_on_result = result

        session.commit()


# ---------------------------------------------------------------------------
# Collector task — fires every minute via redbeat
# ---------------------------------------------------------------------------

@celery_app.task(name="workers.batch_collector.collect_and_dispatch")
def collect_and_dispatch():
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    logger.info("Batch collector running at %s UTC", now_utc.strftime("%H:%M"))

    all_vms = _all_vm_schedules()
    if not all_vms:
        return

    for action in ("on", "off"):
        due_vms = []
        for meta in all_vms:
            now_local = _local_now(meta, now_utc)
            if not _is_due(meta, action, now_local):
                continue
            # Pass the VM-local date explicitly — is_blackout() must not
            # compute its own "today", since the container's local timezone
            # may not match the VM's schedule timezone (see _local_now).
            blocked, reason = is_blackout(meta["vm_id"], today=now_local.date())
            if blocked:
                logger.info("Suppressed power_%s vm=%s reason=%s", action, meta["vm_id"], reason)
                _write_log(meta["vm_id"], meta["provider"], action, "suppressed", reason)
                continue
            due_vms.append(meta)

        if not due_vms:
            continue

        logger.info("power_%s due for %d VMs this minute", action, len(due_vms))

        groups: dict[tuple, list[dict]] = defaultdict(list)
        for meta in due_vms:
            groups[_group_key(meta)].append(meta)

        for group_key, vms in groups.items():
            provider = group_key[0]
            logger.info(
                "Dispatching batch power_%s: provider=%s group=%s count=%d",
                action, provider, group_key[1:], len(vms)
            )
            if provider == "aws":
                aws_batch_power.apply_async(
                    args=(action, vms), expires=TASK_EXPIRY_SECONDS
                )
            elif provider == "azure":
                azure_batch_power.apply_async(
                    args=(action, vms), expires=TASK_EXPIRY_SECONDS
                )
            elif provider == "vmware":
                for vm in vms:
                    vmware_single_power.apply_async(
                        args=(action, vm), expires=TASK_EXPIRY_SECONDS
                    )


# ---------------------------------------------------------------------------
# Batch tasks
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def aws_batch_power(self, action: str, vms: list[dict]):
    from workers.providers.aws import aws_batch_action

    results = aws_batch_action(action, vms)

    failed = {}
    for vm_id, state in results.items():
        provider = next(v["provider"] for v in vms if v["vm_id"] == vm_id)
        if state.startswith("ERROR:"):
            failed[vm_id] = state
            _write_log(vm_id, provider, action, "error", state)
        else:
            _write_log(vm_id, provider, action, "success", state)

    if failed:
        logger.error("AWS batch had %d failures: %s", len(failed), failed)
        failed_meta = [vm for vm in vms if vm["vm_id"] in failed]
        raise self.retry(
            kwargs={"action": action, "vms": failed_meta},
            exc=RuntimeError(f"Partial batch failure: {list(failed.keys())}")
        )

    logger.info("AWS batch power_%s complete: %d VMs", action, len(results))
    return results


@celery_app.task(
    bind=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def azure_batch_power(self, action: str, vms: list[dict]):
    from workers.providers.azure import azure_batch_action

    results = azure_batch_action(action, vms)

    failed = {}
    for vm_id, state in results.items():
        provider = next(v["provider"] for v in vms if v["vm_id"] == vm_id)
        if state.startswith("ERROR:"):
            failed[vm_id] = state
            _write_log(vm_id, provider, action, "error", state)
        else:
            _write_log(vm_id, provider, action, "success", state)

    if failed:
        logger.error("Azure batch had %d failures: %s", len(failed), failed)
        failed_meta = [vm for vm in vms if vm["vm_id"] in failed]
        raise self.retry(
            kwargs={"action": action, "vms": failed_meta},
            exc=RuntimeError(f"Partial batch failure: {list(failed.keys())}")
        )

    logger.info("Azure batch power_%s complete: %d VMs", action, len(results))
    return results


@celery_app.task(
    bind=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def vmware_single_power(self, action: str, vm: dict):
    from workers.providers.vmware import vmware_power_off, vmware_power_on
    from workers.lock import vm_lock, VmLockError

    vm_id = vm["vm_id"]
    try:
        with vm_lock(vm_id, action=f"power_{action}"):
            if action == "off":
                vmware_power_off(**vm)
            else:
                vmware_power_on(**vm)
        _write_log(vm_id, vm["provider"], action, "success")
    except VmLockError:
        logger.warning("VMware power_%s skipped — lock held for vm=%s", action, vm_id)
        _write_log(vm_id, vm["provider"], action, "suppressed", "lock held")
    except Exception as exc:
        _write_log(vm_id, vm["provider"], action, "error", str(exc))
        raise


# ---------------------------------------------------------------------------
# Expiry handling
# ---------------------------------------------------------------------------

from celery.signals import task_revoked


@task_revoked.connect
def _on_task_revoked(request=None, terminated=None, signum=None, expired=None, **kwargs):
    """
    Fired when a task is discarded due to expiry (or manual revocation).
    Writes an "expired" execution_log entry per affected VM so missed
    operations are visible in /schedules/summary rather than vanishing
    silently from the queue.

    Only batch power tasks carry VM-level detail worth logging — the
    collector tick itself (collect_and_dispatch) has no args to extract.
    """
    if not expired:
        return  # manual revocation, not an expiry — nothing to log

    task_name = getattr(request, "task", "") or ""
    if "batch_collector" not in task_name:
        return

    args = getattr(request, "args", None) or ()
    if len(args) != 2:
        return

    action, vms = args
    if task_name.endswith("vmware_single_power"):
        vms = [vms]   # single VM dict, not a list

    if not isinstance(vms, list):
        return

    for vm in vms:
        try:
            logger.warning(
                "Task expired before execution: vm=%s action=power_%s task=%s",
                vm.get("vm_id"), action, task_name
            )
            _write_log(
                vm.get("vm_id"), vm.get("provider", "unknown"),
                action, "expired",
                "Task expired in queue — all workers were unavailable"
            )
        except Exception:
            logger.exception("Failed to write expiry log for vm=%s", vm.get("vm_id"))
