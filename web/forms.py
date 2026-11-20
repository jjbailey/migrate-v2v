#!/usr/bin/env python3
# web/forms.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Web form contract derived from the shell dialogs' exported job variables."""

import re
from dataclasses import dataclass
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
INFRA = {"ARTIFACT_ROOT", "MIGRATE_V2V_VAULT_DIR", "MIGRATE_V2V_CATALOG_DIR"}
CLOUDS = ("aws", "azure", "gcp", "openstack")

@dataclass(frozen=True)
class Field:
    label: str
    kind: str = "text"  # text, boolean, choice, json, path, textarea
    default: str = ""
    required: bool = False
    choices: tuple[str, ...] = ()
    help: str = ""

F = {
    "VM_NAME": Field("Source VM", required=True),
    "VSPHERE_TARGET_VCENTER": Field("Source vCenter"),
    "VCENTER_DATACENTER": Field("Legacy datacenter override", help="Only used without a selected vCenter."),
    "VALIDATE_CERTS": Field("Validate vCenter certificates", "choice", choices=("", "true", "false")),
    "QUIESCE_SNAPSHOT": Field("Quiesce snapshot", "boolean", "true"),
    "REQUIRE_CBT": Field("Require source CBT", "boolean", "false",
                         help="Stop before transfer if Changed Block Tracking is unavailable."),
    "AWS_TARGET_PROJECT": Field("AWS account"),
    "AZURE_TARGET_PROJECT": Field("Azure subscription"),
    "GCP_TARGET_PROJECT": Field("GCP project"),
    "OS_PROJECT_NAME": Field("OpenStack project"),
    "BACKEND": Field("VDDK transport", "choice", "auto", choices=("auto", "vddk", "curl")),
    "DISCARD_RUN_ID": Field("Failed run ID to discard", help="Digits only. This removes the failed run instead of migrating."),
    "VCENTER_CA_BUNDLE": Field("vCenter CA bundle path", "path",
                               help="Select an absolute file under WEB_INPUT_DIR."),
    "COMPRESS": Field("Compress QCOW2 images", "boolean", "false"),
    "GUEST_CONVERT": Field("Convert guest for KVM", "boolean", "false"),
    "VIRTIO_WIN": Field("virtio-win ISO or directory", "path",
                         help="Optional override. If blank, the stable ISO is downloaded to WEB_INPUT_DIR for Windows guest conversion."),
    "INSTANCE_NAME": Field("Instance name", help="Blank derives the name from the source VM."),
    "AWS_SUBNET_ID": Field("Subnet ID", required=True),
    "AWS_SECURITY_GROUPS_JSON": Field("Security groups", "json", "[]", help="JSON array of names or IDs; [] uses the VPC default."),
    "AWS_INSTANCE_TYPE": Field("Instance type", default="t3.medium"),
    "AWS_KEY_NAME": Field("EC2 key pair name", default="cloud"),
    "AZURE_VM_SIZE": Field("VM size", default="Standard_B2s_v2", required=True,
                           help="Azure requires available vCPU quota for this VM size's family in the project's region."),
    "AZURE_VNET_NAME": Field("Virtual network", default="migrate-v2v-vnet", required=True),
    "AZURE_SUBNET_NAME": Field("Subnet", default="migrate-v2v-subnet", required=True),
    "AZURE_NSG_NAME": Field("Network security group", default="migrate-v2v-nsg", required=True),
    "AZURE_NO_PUBLIC_IP": Field("Public IP", "boolean", "false"),
    "AZURE_SSH_SOURCE_PREFIX": Field("Allow SSH from", help="Required for a public IP. Enter a CIDR or service tag; * explicitly allows SSH from anywhere."),
    "AZURE_ADMIN_USERNAME": Field("Admin username", default="azureuser", required=True),
    "AZURE_SSH_KEY_NAME": Field("Azure SSH key resource", default="cloud", required=True),
    "AZURE_SSH_KEY_FILE": Field("SSH public key file", "path",
                                 help="Select an absolute file under WEB_INPUT_DIR."),
    "GCP_ZONE": Field("Zone", default="us-central1-a", required=True),
    "GCP_MACHINE_TYPE": Field("Machine type", default="e2-standard-2", required=True),
    "GCP_NETWORK": Field("Network", default="default", required=True),
    "GCP_SUBNETWORK": Field("Subnetwork", help="Optional. Clear this field to let GCP use the selected network's default."),
    "GCP_TAGS_JSON": Field("Network tags", "json", "[]", help="JSON array of tag names."),
    "GCP_NO_ADDRESS": Field("External IP", "boolean", "false"),
    "GCP_SSH_USER": Field("SSH metadata user", default="cloudinit"),
    "GCP_SSH_KEY_FILE": Field("SSH public key file", "path",
                               help="Select an absolute file under WEB_INPUT_DIR."),
    "OS_FLAVOR": Field("Flavor", default="s1-8", required=True),
    "OS_KEY_NAME": Field("SSH key pair", default="cloud",
                          help="Leave blank to omit keypair injection; the image or user-data must provide guest access."),
    "OS_NETWORK": Field("Network", default="Ext-Net", required=True),
    "OS_NETWORK_ID": Field("Network UUID"),
    "OS_NICS_JSON": Field("Custom NICs", "json", help="Optional JSON array; overrides network selection."),
    "OS_SECURITY_GROUPS_JSON": Field("Security groups", "json", "[]", help="JSON array of names."),
    "OS_SECURITY_GROUP_IDS_JSON": Field("Security group UUIDs", "json", "[]", help="Use UUIDs instead of names when needed."),
    "OS_PRESERVE_MAC": Field("Preserve source MACs", "boolean", "false"),
    "OS_MAC_ADDRESSES_JSON": Field("Manual MAC addresses", "json", help="Optional JSON array, one per NIC."),
    "OS_FIXED_IPS_JSON": Field("Fixed IP addresses", "json", help="Optional JSON array, one per NIC."),
    "OS_AUTO_IP": Field("Request floating IP", "boolean", "true"),
    "OS_CONFIG_DRIVE": Field("Attach config drive", "boolean", "false"),
    "OS_DHCP_USER_DATA": Field("Use bundled DHCP user data", "boolean", "false"),
    "OS_USER_DATA": Field("Inline cloud-init user data", "textarea"),
    "OS_USER_DATA_FILE": Field("Cloud-init file path", "path",
                                help="Select an absolute file under WEB_INPUT_DIR."),
}

def dialog_path(mode: str, cloud: str, method: str = "standard") -> Path:
    if cloud not in CLOUDS or mode not in ("migrate", "launch"):
        raise ValueError("Unknown operation")
    if mode == "launch" and method != "standard":
        raise ValueError("Unknown launch method")
    if mode == "migrate" and method == "vddk" and cloud not in ("azure", "openstack"):
        raise ValueError("VDDK method unavailable")
    if method not in ("standard", "vddk"):
        raise ValueError("Unknown method")
    suffix = "-vddk" if method == "vddk" else ""
    return BIN / f"{mode}-{cloud}{suffix}-dialog.sh"

def exported_fields(mode: str, cloud: str, method: str = "standard") -> tuple[str, ...]:
    path = dialog_path(mode, cloud, method)
    source = path.read_text()
    if mode == "migrate":
        source += (BIN / "migrate-dialog-common.sh").read_text()
        names = [match.group(1) for line in source.splitlines() if not line.lstrip().startswith("#")
                 for match in re.finditer(r"printf 'export ([A-Z_]+)=", line)]
        target = re.search(r"^MIGRATE_TARGET_VAR=([A-Z_]+)$", source, re.M)
        if not target:
            raise ValueError(f"Missing target declaration: {path}")
        names.append(target.group(1))
        names = [n for n in names if n != "MIGRATE_TARGET_VAR"]
        if method != "vddk":
            names = [n for n in names if n not in ("BACKEND", "DISCARD_RUN_ID", "VCENTER_CA_BUNDLE",
                                                  "COMPRESS", "GUEST_CONVERT", "VIRTIO_WIN", "REQUIRE_CBT")]
        elif cloud != "openstack":
            names = [n for n in names if n not in ("COMPRESS", "GUEST_CONVERT", "VIRTIO_WIN")]
    else:
        names = re.findall(r'"([A-Z_]+)=\$[A-Z_]+"', source)
        names += re.findall(r"printf 'export ([A-Z_]+)=", source)
    names = list(dict.fromkeys(n for n in names if n not in INFRA))
    missing = set(names) - F.keys()
    if missing:
        raise ValueError(f"Add web field metadata for {path.name}: {sorted(missing)}")
    if not names or "VM_NAME" not in names:
        raise ValueError(f"Could not parse dialog exports: {path}")
    return tuple(names)

def wrapper_path(mode: str, cloud: str, method: str = "standard") -> Path:
    dialog = dialog_path(mode, cloud, method)
    source = dialog.read_text()
    if mode == "migrate":
        match = re.search(r"^MIGRATE_WRAPPER=([\w.-]+)$", source, re.M)
        if not match:
            raise ValueError(f"Missing wrapper in {dialog}")
        name = match.group(1)
    else:
        matches = re.findall(r'\$script_dir/(launch-[\w.-]+\.sh)', source)
        if not matches:
            raise ValueError(f"Missing wrapper in {dialog}")
        name = matches[-1]
    return BIN / name
