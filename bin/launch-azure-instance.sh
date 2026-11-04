#!/bin/bash
# bin/launch-azure-instance.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -euo pipefail
wrapper_name=launch-azure-instance
export wrapper_name
wrapper_extra_args=()
# shellcheck source=wrapper-common.sh
# shellcheck disable=SC1091
source "$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)/wrapper-common.sh"
wrapper_capture_args "$@"

VM_NAME=${VM_NAME:-}
INSTANCE_NAME=${INSTANCE_NAME:-}
AZURE_TARGET_PROJECT=${AZURE_TARGET_PROJECT:-}
AZURE_VM_SIZE=${AZURE_VM_SIZE:-}
AZURE_VNET_NAME=${AZURE_VNET_NAME:-}
AZURE_SUBNET_NAME=${AZURE_SUBNET_NAME:-}
AZURE_NSG_NAME=${AZURE_NSG_NAME:-}
AZURE_NO_PUBLIC_IP=${AZURE_NO_PUBLIC_IP:-false}
# Azure provisions the admin user through cloud-init with the Azure SSH public
# key resource AZURE_SSH_KEY_NAME in the project's resource group, the way the
# AWS and OpenStack launches use a registered key pair. AZURE_SSH_KEY_FILE, a
# local .pub file, overrides it.
AZURE_ADMIN_USERNAME=${AZURE_ADMIN_USERNAME:-}
AZURE_SSH_KEY_NAME=${AZURE_SSH_KEY_NAME:-cloud}
AZURE_SSH_KEY_FILE=${AZURE_SSH_KEY_FILE:-}

wrapper_require_bool AZURE_NO_PUBLIC_IP "$AZURE_NO_PUBLIC_IP"

if [ -n "$AZURE_SSH_KEY_FILE" ] && [ ! -r "$AZURE_SSH_KEY_FILE" ] ; then
    echo "${wrapper_name}: AZURE_SSH_KEY_FILE is not readable: $AZURE_SSH_KEY_FILE" >&2
    exit 1
fi

args=()
[ -n "$VM_NAME" ] && args+=(-e "$(wrapper_var vm_name "$VM_NAME")")
[ -n "$INSTANCE_NAME" ] && args+=(-e "$(wrapper_var instance_name "$INSTANCE_NAME")")
[ -n "$AZURE_TARGET_PROJECT" ] && args+=(-e "$(wrapper_var azure_target_project "$AZURE_TARGET_PROJECT")")
[ -n "$AZURE_VM_SIZE" ] && args+=(-e "$(wrapper_var azure_vm_size "$AZURE_VM_SIZE")")
[ -n "$AZURE_VNET_NAME" ] && args+=(-e "$(wrapper_var azure_vnet_name "$AZURE_VNET_NAME")")
[ -n "$AZURE_SUBNET_NAME" ] && args+=(-e "$(wrapper_var azure_subnet_name "$AZURE_SUBNET_NAME")")
[ -n "$AZURE_NSG_NAME" ] && args+=(-e "$(wrapper_var azure_nsg_name "$AZURE_NSG_NAME")")
[ "$AZURE_NO_PUBLIC_IP" = true ] && args+=(-e "$(wrapper_var azure_no_public_ip "true")")
[ -n "$AZURE_ADMIN_USERNAME" ] && args+=(-e "$(wrapper_var azure_admin_username "$AZURE_ADMIN_USERNAME")")
[ -n "$AZURE_SSH_KEY_NAME" ] && args+=(-e "$(wrapper_var azure_ssh_key_name "$AZURE_SSH_KEY_NAME")")
[ -n "$AZURE_SSH_KEY_FILE" ] && args+=(-e "$(wrapper_var azure_ssh_public_key_file "$AZURE_SSH_KEY_FILE")")

wrapper_run_playbook playbooks/launch-azure-instance.yml "${args[@]}" "${wrapper_extra_args[@]}" -v
