from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
import ssl
import logging

from workers.vault import get_vcenter_credentials

logger = logging.getLogger(__name__)


def _get_vm(si, vm_id: str):
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    for vm in container.view:
        if vm.config and vm.config.instanceUuid == vm_id:
            return vm
    raise ValueError(f"VM not found: {vm_id}")


def _connect(vcenter_host: str):
    username, password = get_vcenter_credentials()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # TODO: use proper cert in production
    return SmartConnect(
        host=vcenter_host,
        user=username,
        pwd=password,
        sslContext=context,
    )


def vmware_power_off(vm_id: str, vcenter_host: str, **kwargs):
    logger.info("VMware power off: vm_id=%s vcenter=%s", vm_id, vcenter_host)
    si = _connect(vcenter_host)
    try:
        vm = _get_vm(si, vm_id)
        if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOff:
            task = vm.PowerOffVM_Task()
            # Wait for task — pyVmomi task polling
            while task.info.state not in (
                vim.TaskInfo.State.success, vim.TaskInfo.State.error
            ):
                pass
            if task.info.state == vim.TaskInfo.State.error:
                raise RuntimeError(f"VMware power off failed: {task.info.error}")
        logger.info("VMware power off complete: vm_id=%s", vm_id)
    finally:
        Disconnect(si)


def vmware_power_on(vm_id: str, vcenter_host: str, **kwargs):
    logger.info("VMware power on: vm_id=%s vcenter=%s", vm_id, vcenter_host)
    si = _connect(vcenter_host)
    try:
        vm = _get_vm(si, vm_id)
        if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
            task = vm.PowerOnVM_Task()
            while task.info.state not in (
                vim.TaskInfo.State.success, vim.TaskInfo.State.error
            ):
                pass
            if task.info.state == vim.TaskInfo.State.error:
                raise RuntimeError(f"VMware power on failed: {task.info.error}")
        logger.info("VMware power on complete: vm_id=%s", vm_id)
    finally:
        Disconnect(si)
