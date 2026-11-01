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

AZURE_TARGET_PROJECT=$(choose_target "Which Azure subscription should this VM launch into?" vaults/azure.yml azure_projects "${AZURE_TARGET_PROJECT:-}") || abort
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

# Azure has no project-wide key, so every launch needs one.
default_key=${AZURE_SSH_KEY_FILE:-}
if [ -z "$default_key" ] ; then
    for candidate in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub" ; do
        [ -r "$candidate" ] && {
            default_key=$candidate
            break
        }
    done
fi
while : ; do
    AZURE_SSH_KEY_FILE=$(input "SSH public key file for $AZURE_ADMIN_USERNAME:" "$default_key") || abort
    [ -r "$AZURE_SSH_KEY_FILE" ] && {
        AZURE_SSH_KEY_FILE=$(realpath -- "$AZURE_SSH_KEY_FILE")
        break
    }
    bad "That file is not readable: $AZURE_SSH_KEY_FILE" || true
done

say "Launch summary for $VM_NAME

  Subscription.: ${AZURE_TARGET_PROJECT:-auto-select}
  VM name......: ${INSTANCE_NAME:-derived from $VM_NAME}
  VM size......: $AZURE_VM_SIZE
  VNet / subnet: $AZURE_VNET_NAME / $AZURE_SUBNET_NAME
  NSG..........: $AZURE_NSG_NAME
  No public IP.: $AZURE_NO_PUBLIC_IP
  Admin user...: $AZURE_ADMIN_USERNAME
  SSH key......: $AZURE_SSH_KEY_FILE" 17

file=$(mktemp "${TMPDIR:-/tmp}/launch-azure.XXXXXX")
{
    printf '#!/bin/bash\n'
    for pair in "VM_NAME=$VM_NAME" "AZURE_TARGET_PROJECT=$AZURE_TARGET_PROJECT" \
        "AZURE_VM_SIZE=$AZURE_VM_SIZE" "AZURE_VNET_NAME=$AZURE_VNET_NAME" \
        "AZURE_SUBNET_NAME=$AZURE_SUBNET_NAME" "AZURE_NSG_NAME=$AZURE_NSG_NAME" \
        "AZURE_NO_PUBLIC_IP=$AZURE_NO_PUBLIC_IP" "AZURE_ADMIN_USERNAME=$AZURE_ADMIN_USERNAME" \
        "AZURE_SSH_KEY_FILE=$AZURE_SSH_KEY_FILE" "INSTANCE_NAME=$INSTANCE_NAME" ; do
        printf 'export %s=%s\n' "${pair%%=*}" "$(quote "${pair#*=}")"
    done
    printf '%s\n' "$(quote "$script_dir/launch-azure-instance.sh")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME"
