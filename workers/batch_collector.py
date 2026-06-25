"""
batch_collector.py — groups VMs due for a power action and dispatches
one API call per provider group rather than one per VM.

Schedule metadata is read from Postgres (vm_schedules table).
Redis is used only as the Celery broker and redbeat schedule store.
Execution results are written back to Postgres (execution_log table).

Grouping keys:
  AWS:    (provider, role_arn, region)      — one STS assume-role per group
  Azure:  (provider, subscription_id)       — rate limit is per subscription
  VMware: (provider, vcenter_host)          — one vCenter connection per group
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from db import ExecutionLog, VmSchedule, get_session
from workers.celery_app import celery_app
from workers.blackouts import is_blackout
from workers.vault import VaultConfigError

logger = logging.getLogger(__name__)

# Task expiry — if workers are down, stale tasks are dropped rather than
# firing late on recovery. A missed cycle simply doesn't happen.
TASK_EXPIRY_SECONDS     = 300   # 5 minutes for batch power tasks
COLLECTOR_EXPIRY_SECONDS = 60   # 1 minute for the collector tick itself


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_vm_schedules() -> list[dict]:
    """Fetch all VM schedules as plain dicts. Single SELECT, no filtering."""
    with get_session() as session:
        rows = session.query(VmSchedule).all()
        return [row.to_dict() for row in rows]


def _local_now(meta: dict, now_utc: datetime) -> datetime:
    """
    Convert UTC now to the VM's local timezone. Used for both due-time
    and blackout checks so both always agree on the current local date/time,
    regardless of the container's own timezone setting.
    """
    tz = ZoneInfo(meta.get("timezone") or "UTC")
    return now_utc.astimezone(tz)


def _is_24x7(meta: dict) -> bool:
    """24x7 sentinel — both hours are 0, VM should never be power-cycled."""
    return (
        meta.get("power_on_hour",  0) == 0 and
        meta.get("power_off_hour", 0) == 0
    )


def _is_due(meta: dict, action: str, now_local: datetime) -> bool:
    """
    Return True if this VM's scheduled action is due in the current minute,
    evaluated in the VM's own timezone (now_local — see _local_now).
    Weekday/weekend filtering is handled downstream by is_blackout().
    """
    if _is_24x7(meta):
        return False

    scheduled_hour   = meta.get(f"power_{action}_hour",   0)
    scheduled_minute = meta.get(f"power_{action}_minute", 0)

    return (
        now_local.hour   == scheduled_hour and
        now_local.minute == scheduled_minute
    )


def _group_key(meta: dict) -> tuple:
    """
    Determines which VMs can be batched together in a single API call.
    All fields come from provider_config JSONB column.
    """
    provider = meta["provider"]
    config   = meta.get("provider_config") or {}

    if provider == "aws":
        return (provider, config.get("role_arn", ""), config.get("region", ""))
    if provider == "azure":
        return (provider, config.get("subscription_id", ""))
    return (provider, config.get("vcenter_host", ""))


def _write_log(vm_id: str, display_name: str | None, provider: str,
               action: str, result: str, detail: str | None = None):
    """
    Append one row to execution_log and update summary columns on vm_schedules.
    """
    now = datetime.now(timezone.utc)
    with get_session() as session:
        session.add(ExecutionLog(
            vm_id=vm_id,
            display_name=display_name,
            provider=provider,
            action=action,
            result=result,
            detail=detail,
            executed_at=now,
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

@celery_app.task(
    name="workers.batch_collector.collect_and_dispatch",
    expires=COLLECTOR_EXPIRY_SECONDS,
)
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
            # Pass VM-local date explicitly — is_blackout() must not
            # compute its own "today" since the container timezone may differ
            blocked, reason = is_blackout(meta["vm_id"], today=now_local.date())
            if blocked:
                logger.info(
                    "Suppressed power_%s vm=%s reason=%s",
                    action, meta["vm_id"], reason
                )
                _write_log(
                    meta["vm_id"], meta.get("display_name"),
                    meta["provider"], action, "suppressed", reason
                )
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

    # VaultConfigError is a configuration problem — retrying won't fix it.
    # Fail immediately rather than burning through max_retries.
    try:
        results = aws_batch_action(action, vms)
    except VaultConfigError:
        raise   # bypass autoretry — goes straight to task_failure signal

    failed = {}
    for vm_id, state in results.items():
        meta = next((v for v in vms if v["vm_id"] == vm_id), {})
        if state.startswith("ERROR:"):
            failed[vm_id] = state
            _write_log(vm_id, meta.get("display_name"),
                       meta.get("provider", "aws"), action, "error", state)
        else:
            _write_log(vm_id, meta.get("display_name"),
                       meta.get("provider", "aws"), action, "success", state)

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

    try:
        results = azure_batch_action(action, vms)
    except VaultConfigError:
        raise   # bypass autoretry — goes straight to task_failure signal

    failed = {}
    for vm_id, state in results.items():
        meta = next((v for v in vms if v["vm_id"] == vm_id), {})
        if state.startswith("ERROR:"):
            failed[vm_id] = state
            _write_log(vm_id, meta.get("display_name"),
                       meta.get("provider", "azure"), action, "error", state)
        else:
            _write_log(vm_id, meta.get("display_name"),
                       meta.get("provider", "azure"), action, "success", state)

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

    vm_id        = vm["vm_id"]
    display_name = vm.get("display_name")
    provider_cfg = vm.get("provider_config") or {}

    try:
        with vm_lock(vm_id, action=f"power_{action}"):
            if action == "off":
                vmware_power_off(vm_id=vm_id, provider_config=provider_cfg)
            else:
                vmware_power_on(vm_id=vm_id, provider_config=provider_cfg)
        _write_log(vm_id, display_name, vm.get("provider", "vmware"),
                   action, "success")
    except VaultConfigError:
        raise   # bypass autoretry — goes straight to task_failure signal
    except VmLockError:
        logger.warning(
            "VMware power_%s skipped — lock held for vm=%s", action, vm_id
        )
        _write_log(vm_id, display_name, vm.get("provider", "vmware"),
                   action, "suppressed", "lock held")
    except Exception as exc:
        _write_log(vm_id, display_name, vm.get("provider", "vmware"),
                   action, "error", str(exc))
        raise


# ---------------------------------------------------------------------------
# Failure / expiry signal handlers
# ---------------------------------------------------------------------------

from celery.signals import task_failure, task_revoked


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None,
                     args=None, **kwargs):
    """
    Catch permanent failures (after all retries exhausted) for connection/
    credential-level errors that bypass per-VM _write_log calls.
    """
    task_name = getattr(sender, "name", "") or ""
    if "batch_collector" not in task_name:
        return
    if task_name.endswith("collect_and_dispatch"):
        return

    # Partial batch failures already logged per-VM — don't double-log
    if isinstance(exception, RuntimeError) and "Partial batch failure" in str(exception):
        return

    if not args or len(args) != 2:
        return

    action, vms = args
    if task_name.endswith("vmware_single_power"):
        vms = [vms]
    if not isinstance(vms, list):
        return

    detail = f"ERROR: {exception}" if exception else "Task failed permanently after retries"

    for vm in vms:
        try:
            logger.error(
                "Task failed permanently: vm=%s action=power_%s task=%s error=%s",
                vm.get("vm_id"), action, task_name, exception
            )
            _write_log(
                vm.get("vm_id"), vm.get("display_name"),
                vm.get("provider", "unknown"), action, "error", detail
            )
        except Exception:
            logger.exception(
                "Failed to write failure log for vm=%s", vm.get("vm_id")
            )


@task_revoked.connect
def _on_task_revoked(request=None, terminated=None,
                     signum=None, expired=None, **kwargs):
    """Write 'expired' log entries when tasks are dropped due to expiry."""
    if not expired:
        return

    task_name = getattr(request, "task", "") or ""
    if "batch_collector" not in task_name:
        return

    args = getattr(request, "args", None) or ()
    if len(args) != 2:
        return

    action, vms = args
    if task_name.endswith("vmware_single_power"):
        vms = [vms]
    if not isinstance(vms, list):
        return

    for vm in vms:
        try:
            logger.warning(
                "Task expired before execution: vm=%s action=power_%s",
                vm.get("vm_id"), action
            )
            _write_log(
                vm.get("vm_id"), vm.get("display_name"),
                vm.get("provider", "unknown"), action, "expired",
                "Task expired in queue — all workers were unavailable"
            )
        except Exception:
            logger.exception(
                "Failed to write expiry log for vm=%s", vm.get("vm_id")
            )
