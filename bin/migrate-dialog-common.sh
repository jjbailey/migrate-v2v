#!/bin/bash
# bin/migrate-dialog-common.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Shared questions and command generation for the migration dialogs. Callers
# set MIGRATE_TARGET_VAR, MIGRATE_TARGET_LABEL, MIGRATE_VAULT,
# MIGRATE_VAULT_MAP, and MIGRATE_WRAPPER before sourcing this file.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
# shellcheck source=launch-dialog-common.sh
source "$script_dir/launch-dialog-common.sh"
MIGRATE_VAULT="$MIGRATE_V2V_VAULT_DIR/${MIGRATE_VAULT##*/}"

VSPHERE_TARGET_VCENTER=$(choose_target "Which source vCenter is this VM on?" "$MIGRATE_V2V_VAULT_DIR/vsphere.yml" vsphere_projects "${VSPHERE_TARGET_VCENTER:-}") || abort
if [ -z "$VSPHERE_TARGET_VCENTER" ] ; then
    VCENTER_DATACENTER=$(input "Legacy datacenter override (blank for the playbook default):" "${VCENTER_DATACENTER:-}") || abort
else
    VCENTER_DATACENTER=""
fi

while : ; do
    VM_NAME=$(input "Source VM name in vCenter:" "${VM_NAME:-}") || abort
    [ -n "$VM_NAME" ] && break
    bad "The VM name cannot be empty." || true
done

target_value=$(choose_target "Which $MIGRATE_TARGET_LABEL should this VM migrate into?" "$MIGRATE_VAULT" "$MIGRATE_VAULT_MAP" "${!MIGRATE_TARGET_VAR:-}") || abort
printf -v "$MIGRATE_TARGET_VAR" '%s' "$target_value"

VALIDATE_CERTS=$(tristate "Override vCenter certificate validation?" "${VALIDATE_CERTS:-}") || abort

quiesce_default=yes
[ "${QUIESCE_SNAPSHOT:-true}" = false ] && quiesce_default=no
if yesno "Quiesce the VM for a consistent snapshot?\n\nRecommended unless the guest doesn't support quiescing." "$quiesce_default" ; then
    QUIESCE_SNAPSHOT=true
else
    QUIESCE_SNAPSHOT=false
fi

if [ "$MIGRATE_WRAPPER" = migrate-openstack-vm-vddk.sh ] ; then
    compress_default=no
    [ "${COMPRESS:-false}" = true ] && compress_default=yes
    if yesno "Compress the QCOW2 images before uploading them?\n\nThis can reduce upload size on a slow link but uses more CPU." "$compress_default" ; then
        COMPRESS=true
    else
        COMPRESS=false
    fi
fi

method=""
case "$MIGRATE_WRAPPER" in *-vddk.sh) method=" (datastore-direct)" ;; esac
summary="Migration summary for $VM_NAME$method

  Source vCenter..: ${VSPHERE_TARGET_VCENTER:-${VCENTER_DATACENTER:-auto-select}}
  Target..........: ${!MIGRATE_TARGET_VAR:-auto-select}
  Validate certs..: ${VALIDATE_CERTS:-vault default}
  Artifact root...: $ARTIFACT_ROOT
  Quiesce snapshot: $QUIESCE_SNAPSHOT"
[ "$MIGRATE_WRAPPER" != migrate-openstack-vm-vddk.sh ] || summary="$summary
  Compress images.: $COMPRESS"
say "$summary" 16

file=$(mktemp "${TMPDIR:-/tmp}/${DIALOG_NAME}.XXXXXX")
{
    printf '#!/bin/bash\n'
    printf 'export ARTIFACT_ROOT=%s\n' "$(quote "$ARTIFACT_ROOT")"
    printf 'export MIGRATE_V2V_VAULT_DIR=%s\n' "$(quote "$MIGRATE_V2V_VAULT_DIR")"
    printf 'export MIGRATE_V2V_CATALOG_DIR=%s\n' "$(quote "$MIGRATE_V2V_CATALOG_DIR")"
    printf 'export VM_NAME=%s\n' "$(quote "$VM_NAME")"
    printf 'export VCENTER_DATACENTER=%s\n' "$(quote "$VCENTER_DATACENTER")"
    printf 'export VSPHERE_TARGET_VCENTER=%s\n' "$(quote "$VSPHERE_TARGET_VCENTER")"
    printf 'export %s=%s\n' "$MIGRATE_TARGET_VAR" "$(quote "$target_value")"
    printf 'export VALIDATE_CERTS=%s\n' "$(quote "$VALIDATE_CERTS")"
    printf 'export QUIESCE_SNAPSHOT=%s\n' "$(quote "$QUIESCE_SNAPSHOT")"
    [ "$MIGRATE_WRAPPER" != migrate-openstack-vm-vddk.sh ] || printf 'export COMPRESS=%s\n' "$(quote "$COMPRESS")"
    printf '%s\n' "$(quote "$script_dir/$MIGRATE_WRAPPER")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME" migrate
