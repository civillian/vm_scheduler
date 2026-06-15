"""
Azure provider — per-subscription with Retry-After handling.

Azure does not support batching VM power operations, so we run them
concurrently within each subscription using a thread pool, while
respecting the Retry-After header on 429 responses.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient

logger = logging.getLogger(__name__)

# Conservative concurrency per subscription — stay well under the
# ~0.33 RPS sustained limit by not hammering all at once.
# At 8 concurrent with ~3s per op = ~2.6 RPS burst, then back off on 429.
AZURE_CONCURRENCY_PER_SUB = 8

# Cap on how long we'll honour a Retry-After before giving up and
# letting Celery's own retry/backoff take over
MAX_RETRY_AFTER_SECONDS = 120


def _compute_client(subscription_id: str) -> ComputeManagementClient:
    credential = DefaultAzureCredential()
    return ComputeManagementClient(credential, subscription_id)


def _do_single_action(
    client: ComputeManagementClient,
    action: Literal["start", "stop"],
    vm_id: str,
    resource_group: str,
) -> str:
    """
    Perform a single VM start or deallocate, with Retry-After handling.
    Returns the final state string or raises on unrecoverable error.
    """
    attempts = 0
    while True:
        try:
            attempts += 1
            if action == "stop":
                poller = client.virtual_machines.begin_deallocate(resource_group, vm_id)
            else:
                poller = client.virtual_machines.begin_start(resource_group, vm_id)
            poller.result()
            return "deallocated" if action == "stop" else "running"

        except HttpResponseError as exc:
            if exc.status_code == 429:
                retry_after = int(
                    exc.response.headers.get("Retry-After", 30)
                )
                if retry_after > MAX_RETRY_AFTER_SECONDS:
                    logger.warning(
                        "Azure 429 Retry-After=%ds exceeds cap for vm=%s — "
                        "deferring to Celery retry", retry_after, vm_id
                    )
                    raise   # let Celery backoff handle it
                logger.warning(
                    "Azure 429 for vm=%s — honouring Retry-After=%ds (attempt %d)",
                    vm_id, retry_after, attempts
                )
                time.sleep(retry_after)
                continue  # retry the same VM
            raise


def azure_batch_action(
    action: Literal["start", "stop"],
    vms: list[dict],   # each: {vm_id, subscription_id, resource_group, ...}
) -> dict[str, str]:
    """
    Execute start or deallocate for a list of VMs sharing the same subscription.
    Runs up to AZURE_CONCURRENCY_PER_SUB in parallel, honouring Retry-After.

    Returns {vm_id: state_or_error}.
    """
    if not vms:
        return {}

    subscription_id = vms[0]["subscription_id"]
    logger.info(
        "Azure batch %s: subscription=%s count=%d",
        action, subscription_id, len(vms)
    )

    # One client per subscription — DefaultAzureCredential is thread-safe
    client = _compute_client(subscription_id)
    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=AZURE_CONCURRENCY_PER_SUB) as pool:
        futures = {
            pool.submit(
                _do_single_action,
                client,
                action,
                vm["vm_id"],
                vm["resource_group"],
            ): vm["vm_id"]
            for vm in vms
        }

        for future in as_completed(futures):
            vm_id = futures[future]
            try:
                state = future.result()
                results[vm_id] = state
                logger.info("Azure %s complete: vm=%s state=%s", action, vm_id, state)
            except Exception as exc:
                logger.error("Azure %s failed: vm=%s error=%s", action, vm_id, exc)
                results[vm_id] = f"ERROR: {exc}"

    return results
