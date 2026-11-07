#!/usr/bin/env python3
# bin/enable-cbt.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Enable Changed Block Tracking on a vSphere VM without power-cycling it.

Sets config.changeTrackingEnabled with a ReconfigVM task, which vCenter allows
on a running VM, so the guest is not shut down. Setting the ctkEnabled
ExtraConfig keys instead does not work: vCenter ignores them and config.changeTrackingEnabled stays false.
Idempotent: a VM that already tracks changes is left alone.

Credentials come from VCENTER_USERNAME and VCENTER_PASSWORD when both are
exported, with VCENTER_HOSTNAME (or --vcenter) naming the host. Otherwise
they come from $MIGRATE_V2V_VAULT_DIR/vsphere.yml, selected the way select-vcenter.yml does:
--vcenter names a vsphere_projects key or vcenter_hostname; without it, the
legacy flat vcenter_* keys, else the only entry. A vault entry also supplies
the datacenter and validate_certs. Needs pyVmomi, which community.vmware
already requires.
"""

import argparse
import os
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vault_common import default_vault_path, read_vault  # noqa: E402

from pyVim.connect import Disconnect, SmartConnect
from pyVim.task import WaitForTask
from pyVmomi import vim, vmodl

def find_vm(content, datacenter, name):
    """Return the one VM called name under the datacenter, or raise ValueError."""
    # Datacenters may sit inside folders, so search the whole inventory for them.
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datacenter], True)
    try:
        datacenters = [dc for dc in view.view if dc.name == datacenter]
    finally:
        view.Destroy()
    if len(datacenters) != 1:
        raise ValueError(f"expected exactly one datacenter named {datacenter!r}, found {len(datacenters)}")
    view = content.viewManager.CreateContainerView(datacenters[0].vmFolder, [vim.VirtualMachine], True)
    try:
        matches = [vm for vm in view.view if vm.name == name]
    finally:
        view.Destroy()
    if len(matches) != 1:
        raise ValueError(f"expected exactly one VM named {name!r} in {datacenter!r}, found {len(matches)}")
    return matches[0]

def vault_vcenter(vault, requested):
    """(name, entry) for the vCenter to use, or (None, None) without one.

    Mirrors select-vcenter.yml. A vault that exists but will not decrypt
    stops the run rather than falling back to the environment.
    """
    if not Path(vault).exists():
        return None, None
    data = read_vault(vault) or {}
    projects = {str(key): value or {} for key, value in (
        data.get("vsphere_projects") or {}).items()}
    if requested:
        for key, entry in projects.items():
            if requested in {key, str(entry.get("vcenter_hostname", ""))}:
                return key, entry
        if requested == str(data.get("vcenter_hostname", "")):
            return "legacy", data
        return None, None
    if str(data.get("vcenter_hostname", "") or "").strip():
        return "legacy", data
    if len(projects) == 1:
        return next(iter(projects.items()))
    return None, None

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vcenter",
                        help="vsphere_projects key or vCenter hostname")
    parser.add_argument("--datacenter",
                        help="defaults to the vault entry's datacenter")
    parser.add_argument("--vm", required=True)
    parser.add_argument("--insecure", action="store_true", help="do not verify the vCenter certificate")
    parser.add_argument("--vault", type=Path, default=default_vault_path("vsphere.yml"),
                        help="vSphere vault for credentials when VCENTER_* is unset "
                             "(default: $MIGRATE_V2V_VAULT_DIR/vsphere.yml, or "
                             "/work/migrate-v2v/vaults/vsphere.yml)")
    args = parser.parse_args()
    env_user = os.environ.get("VCENTER_USERNAME")
    env_password = os.environ.get("VCENTER_PASSWORD")
    if env_user and env_password:
        name, entry = None, None
    else:
        name, entry = vault_vcenter(args.vault, args.vcenter)
    if entry is not None:
        host = str(entry.get("vcenter_hostname", "") or "")
        user = str(entry.get("vcenter_username", "") or "")
        password = str(entry.get("vcenter_password", "") or "")
        datacenter = args.datacenter or str(entry.get("datacenter", "") or "")
        insecure = args.insecure or str(entry.get("validate_certs", True)).lower() in {"false", "no", "0"}
        if not (host and user and password):
            raise SystemExit(f"enable-cbt: vault entry '{name}' is missing vcenter_hostname, "
                             "vcenter_username or vcenter_password")
    else:
        host = args.vcenter or os.environ.get("VCENTER_HOSTNAME")
        user, password = env_user, env_password
        datacenter = args.datacenter
        insecure = args.insecure
        if not (host and user and password):
            raise SystemExit("enable-cbt: no vCenter selected from the vault; set "
                             "VCENTER_HOSTNAME (or --vcenter), VCENTER_USERNAME and "
                             "VCENTER_PASSWORD, or name a vault entry with --vcenter")
    if not datacenter:
        raise SystemExit("enable-cbt: --datacenter is required")

    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        service = SmartConnect(host=host, user=user, pwd=password, sslContext=context)
    except (OSError, ssl.SSLError, vmodl.MethodFault) as error:
        raise SystemExit(f"enable-cbt: {error}")
    try:
        vm = find_vm(service.RetrieveContent(), datacenter, args.vm)
        if vm.config.changeTrackingEnabled:
            print(f"CBT is already enabled on {args.vm}")
            return
        WaitForTask(vm.ReconfigVM_Task(vim.vm.ConfigSpec(changeTrackingEnabled=True)))
        if not vm.config.changeTrackingEnabled:
            raise SystemExit("enable-cbt: vCenter finished the task but CBT is still disabled")
        print(f"CBT enabled on {args.vm}")
    except (OSError, ValueError, vmodl.MethodFault) as error:
        raise SystemExit(f"enable-cbt: {error}")
    finally:
        Disconnect(service)

if __name__ == "__main__":
    sys.exit(main())
