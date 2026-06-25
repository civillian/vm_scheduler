"""
Azure provider — per-subscription with Retry-After handling.

Credentials flow:
  1. Extract vault_role and tenant_id from vm.provider_config
  2. Call Vault Azure static secrets engine to get client_id/client_secret
  3. Authenticate to Azure using ClientSecretCredential
  4. Run begin_deallocate (off) or begin_start (on) per VM

All provider-specific fields come from vm.provider_config:
  {"tenant_id":       "...",
   "subscription_id": "...",
   "resource_group":  "...",
   "vault_role":      "terraform-workspace-name"}
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from azure.core.exceptions import HttpResponseError
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient

from workers.vault import get_azure_credentials

logger = logging.getLogger(__name__)

AZURE_CONCURRENCY_PER_SUB = 8
MAX_RETRY_AFTER_SECONDS   = 120


def _compute_client(tenant_id: str, client_id: str,
                    client_secret: str, subscription_id: str) -> ComputeManagementClient:
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    return ComputeManagementClient(credential, subscription_id)


def _do_single_action(client: ComputeManagementClient, action: str,
                      vm_id: str, resource_group: str) -> str:
    attempts = 0
    while True:
        try:
            attempts += 1
            if action == "off":
                poller = client.virtual_machines.begin_deallocate(resource_group, vm_id)
                poller.result()
                return "deallocated"
            else:
                poller = client.virtual_machines.begin_start(resource_group, vm_id)
                poller.result()
                return "running"

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
                    raise
                logger.warning(
                    "Azure 429 for vm=%s — honouring Retry-After=%ds (attempt %d)",
                    vm_id, retry_after, attempts
                )
                time.sleep(retry_after)
                continue
            raise


def azure_batch_action(action: str, vms: list[dict]) -> dict[str, str]:
    """
    Execute power_on or power_off for a list of VMs sharing the same
    subscription_id. Fetches credentials from Vault per batch.
    Returns {vm_id: state_or_error}.
    """
    if not vms:
        return {}

    config          = vms[0].get("provider_config") or {}
    subscription_id = config.get("subscription_id", "")
    vault_role      = config.get("vault_role", "")
    tenant_id       = config.get("tenant_id", "")

    logger.info(
        "Azure batch power_%s: subscription=%s vault_role=%s count=%d",
        action, subscription_id, vault_role, len(vms)
    )

    # Fetch credentials from Vault Azure static secrets engine
    tenant_id, client_id, client_secret = get_azure_credentials(
        vault_role=vault_role,
        tenant_id=tenant_id,
    )

    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=AZURE_CONCURRENCY_PER_SUB) as pool:
        futures = {
            pool.submit(
                _do_single_action,
                _compute_client(tenant_id, client_id,
                                client_secret, subscription_id),
                action,
                vm["vm_id"],
                (vm.get("provider_config") or {}).get("resource_group", ""),
            ): vm["vm_id"]
            for vm in vms
        }

        for future in as_completed(futures):
            vm_id = futures[future]
            try:
                state = future.result()
                results[vm_id] = state
                logger.info(
                    "Azure power_%s complete: vm=%s state=%s",
                    action, vm_id, state
                )
            except Exception as exc:
                logger.error(
                    "Azure power_%s failed: vm=%s error=%s",
                    action, vm_id, exc
                )
                results[vm_id] = f"ERROR: {exc}"

    return results
