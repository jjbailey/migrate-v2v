#!/bin/bash
# bin/launch-azure-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Dialog front end for bin/launch-azure-instance.sh. Nothing launches until the
# final confirmation.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
DIALOG_NAME=launch-azure-dialog
# shellcheck source=launch-dialog-common.sh
source "$script_dir/launch-dialog-common.sh"

# Try to list the Azure SSH public key resources for PROJECT, for the
# single-select picker. Prints tag\item\status lines for radio(). CURRENT
# pre-selects the existing value. Returns 1 when the lookup fails so the
# caller falls back to manual entry. stdin is /dev/null so a missing vault
# password fails fast instead of prompting inside the dialog.
load_azure_keypair_choices()
{
    local project=$1 current=${2:-} facts args=()
    [ -x "$script_dir/azure-project-facts.py" ] || return 1
    [ -n "$project" ] && args+=("$project")
    args+=(--keypairs)
    if command -v timeout > /dev/null 2>&1 ; then
        facts=$(timeout 60 "$script_dir/azure-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    else
        facts=$("$script_dir/azure-project-facts.py" "${args[@]}" --json < /dev/null 2> /dev/null) || return 1
    fi
    [ -n "$facts" ] || return 1
    python3 -c '
import json, sys
try:
    facts = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
cur = sys.argv[2] if len(sys.argv) > 2 else ""
out = []
for pair in facts.get("keypairs") or []:
    name = str(pair.get("name") or "").strip()
    if not name:
        continue
    detail = ", ".join([str(part) for part in
                         (pair.get("type"), pair.get("fingerprint"))
                         if part]) or "SSH public key"
    if cur:
        status = "ON" if name == cur else "OFF"
    else:
        status = "ON" if name == "cloud" else "OFF"
    out.append("%s\t%s\t%s" % (name, detail, status))
if not out:
    raise SystemExit(1)
if cur and not any(row.split("\t")[0] == cur for row in out):
    out.append("%s\t%s\t%s" % (cur, cur + " (current value, not in list)", "ON"))
print("\n".join(out))' "$facts" "$current" 2> /dev/null
}

AZURE_TARGET_PROJECT=$(choose_target "Which Azure subscription should this VM launch into?" "$MIGRATE_V2V_VAULT_DIR/azure.yml" azure_projects "${AZURE_TARGET_PROJECT:-}") || abort
VM_NAME=$(choose_vm_required azure Azure) || abort

INSTANCE_NAME=$(input "Azure VM name (blank to derive it from $VM_NAME):" "${INSTANCE_NAME:-}") || abort
AZURE_VM_SIZE=$(input_required "VM size:" "${AZURE_VM_SIZE:-Standard_B2s_v2}" "The VM size cannot be empty.") || abort

# The playbook creates these in the vault's resource group only when missing,
# so the defaults give every launch one shared network.
AZURE_VNET_NAME=${AZURE_VNET_NAME:-migrate-v2v-vnet}
AZURE_SUBNET_NAME=${AZURE_SUBNET_NAME:-migrate-v2v-subnet}
AZURE_NSG_NAME=${AZURE_NSG_NAME:-migrate-v2v-nsg}
if ! yesno "Use the shared launch network?\n\n  VNet..: $AZURE_VNET_NAME\n  Subnet: $AZURE_SUBNET_NAME\n  NSG...: $AZURE_NSG_NAME\n\nEach is created when missing (the NSG allows SSH)." yes ; then
    AZURE_VNET_NAME=$(input_required "Virtual network name:" "$AZURE_VNET_NAME" "The virtual network name cannot be empty.") || abort
    AZURE_SUBNET_NAME=$(input_required "Subnet name:" "$AZURE_SUBNET_NAME" "The subnet name cannot be empty.") || abort
    AZURE_NSG_NAME=$(input_required "Network security group name:" "$AZURE_NSG_NAME" "The network security group name cannot be empty.") || abort
fi
public_ip_default=no
[ "${AZURE_NO_PUBLIC_IP:-false}" = true ] && public_ip_default=yes
if yesno "Launch without a public IP address?" "$public_ip_default" ; then AZURE_NO_PUBLIC_IP=true; else AZURE_NO_PUBLIC_IP=false; fi

AZURE_ADMIN_USERNAME=$(input_required "Admin user cloud-init creates:" "${AZURE_ADMIN_USERNAME:-azureuser}" "The admin user cannot be empty.") || abort

# Like the AWS and OpenStack key pairs, the key is an SSH public key resource
# registered in the subscription's resource group. Azure provisions the admin
# user with it, so unlike AWS there is no "no key" choice.
key_choices=$(load_azure_keypair_choices "$AZURE_TARGET_PROJECT" "${AZURE_SSH_KEY_NAME:-cloud}") || key_choices=""
picked_key=_other
if [ -n "$key_choices" ] ; then
    key_args=(_other "Type a different key name manually (skip the list)" OFF)
    while IFS=$'\t' read -r key_tag key_label key_state ; do
        [ -n "$key_tag" ] || continue
        key_args+=("$key_tag" "$key_label" "$key_state")
    done <<< "$key_choices"
    picked_key=$(radio "Which Azure SSH public key should $AZURE_ADMIN_USERNAME use?\n(Select Type manually to enter a known value.)" "${key_args[@]}") || abort
else
    say "Could not list SSH public keys for '${AZURE_TARGET_PROJECT:-auto-select}'.\n\nEnter the key name manually instead." 9 || true
fi
if [ "$picked_key" = _other ] ; then
    AZURE_SSH_KEY_NAME=$(input_required "Azure SSH public key resource for $AZURE_ADMIN_USERNAME:" "${AZURE_SSH_KEY_NAME:-cloud}" "The SSH key name cannot be empty.") || abort
else
    AZURE_SSH_KEY_NAME=$picked_key
fi

say "Launch summary for $VM_NAME

  Subscription.: ${AZURE_TARGET_PROJECT:-auto-select}
  VM name......: ${INSTANCE_NAME:-derived from $VM_NAME}
  VM size......: $AZURE_VM_SIZE
  VNet / subnet: $AZURE_VNET_NAME / $AZURE_SUBNET_NAME
  NSG..........: $AZURE_NSG_NAME
  No public IP.: $AZURE_NO_PUBLIC_IP
  Admin user...: $AZURE_ADMIN_USERNAME
  SSH key......: $AZURE_SSH_KEY_NAME" 17

file=$(mktemp "${TMPDIR:-/tmp}/launch-azure.XXXXXX")
{
    printf '#!/bin/bash\n'
    for pair in "VM_NAME=$VM_NAME" "AZURE_TARGET_PROJECT=$AZURE_TARGET_PROJECT" \
        "MIGRATE_V2V_VAULT_DIR=$MIGRATE_V2V_VAULT_DIR" "MIGRATE_V2V_CATALOG_DIR=$MIGRATE_V2V_CATALOG_DIR" \
        "AZURE_VM_SIZE=$AZURE_VM_SIZE" "AZURE_VNET_NAME=$AZURE_VNET_NAME" \
        "AZURE_SUBNET_NAME=$AZURE_SUBNET_NAME" "AZURE_NSG_NAME=$AZURE_NSG_NAME" \
        "AZURE_NO_PUBLIC_IP=$AZURE_NO_PUBLIC_IP" "AZURE_ADMIN_USERNAME=$AZURE_ADMIN_USERNAME" \
        "AZURE_SSH_KEY_NAME=$AZURE_SSH_KEY_NAME" "AZURE_SSH_KEY_FILE=" \
        "INSTANCE_NAME=$INSTANCE_NAME" ; do
        printf 'export %s=%s\n' "${pair%%=*}" "$(quote "${pair#*=}")"
    done
    printf '%s\n' "$(quote "$script_dir/launch-azure-instance.sh")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME"
