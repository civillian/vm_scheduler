"""
VMware provider — per-VM dispatch with vCenter lock.

Credentials flow:
  1. Fetch vCenter username/password from Vault KV v2
  2. Connect to vCenter specified in vm.provider_config
  3. Execute PowerOnVM_Task or PowerOffVM_Task

All provider-specific fields come from vm.provider_config:
  {"vcenter_host": "vcenter.internal.example.com"}
"""

import logging
import ssl

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

from workers.vault import get_vcenter_credentials

logger = logging.getLogger(__name__)


def _get_vm(si, vm_id: str):
    content   = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    for vm in container.view:
        if vm.config and vm.config.instanceUuid == vm_id:
            return vm
    raise ValueError(f"VM not found in vCenter: {vm_id}")


def _connect(vcenter_host: str):
    username, password = get_vcenter_credentials()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode    = ssl.CERT_NONE  # TODO: use proper cert
    return SmartConnect(
        host=vcenter_host,
        user=username,
        pwd=password,
        sslContext=context,
    )


def _wait_for_task(task):
    while task.info.state not in (
        vim.TaskInfo.State.success,
        vim.TaskInfo.State.error,
    ):
        pass
    if task.info.state == vim.TaskInfo.State.error:
        raise RuntimeError(f"vCenter task failed: {task.info.error}")


def vmware_power_off(vm_id: str, provider_config: dict, **kwargs):
    vcenter_host = (provider_config or {}).get("vcenter_host", "")
    if not vcenter_host:
        raise ValueError("vcenter_host missing from provider_config")

    logger.info("VMware power_off: vm_id=%s vcenter=%s", vm_id, vcenter_host)
    si = _connect(vcenter_host)
    try:
        vm = _get_vm(si, vm_id)
        if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOff:
            _wait_for_task(vm.PowerOffVM_Task())
        logger.info("VMware power_off complete: vm_id=%s", vm_id)
    finally:
        Disconnect(si)


def vmware_power_on(vm_id: str, provider_config: dict, **kwargs):
    vcenter_host = (provider_config or {}).get("vcenter_host", "")
    if not vcenter_host:
        raise ValueError("vcenter_host missing from provider_config")

    logger.info("VMware power_on: vm_id=%s vcenter=%s", vm_id, vcenter_host)
    si = _connect(vcenter_host)
    try:
        vm = _get_vm(si, vm_id)
        if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
            _wait_for_task(vm.PowerOnVM_Task())
        logger.info("VMware power_on complete: vm_id=%s", vm_id)
    finally:
        Disconnect(si)
