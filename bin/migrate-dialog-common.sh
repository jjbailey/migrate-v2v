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

preflight_default=no
[ "${PREFLIGHT_ONLY:-false}" = true ] && preflight_default=yes
if yesno "Run preflight checks only?\n\nChecks source access, target permissions, and local artifact space, then stops before migration setup." "$preflight_default" ; then
    PREFLIGHT_ONLY=true
else
    PREFLIGHT_ONLY=false
fi

if [[ $MIGRATE_WRAPPER == *-vddk.sh ]] ; then
    cbt_default=no
    [ "${REQUIRE_CBT:-false}" = true ] && cbt_default=yes
    if yesno "Require Changed Block Tracking (CBT) on the source VM?\n\nWhen enabled, the migration stops before transfer if CBT is unavailable." "$cbt_default" ; then
        REQUIRE_CBT=true
    else
        REQUIRE_CBT=false
    fi

    backend_default=auto
    [ -n "${BACKEND:-}" ] && backend_default="$BACKEND"
    backend_auto=OFF backend_vddk=OFF backend_curl=OFF
    case "$backend_default" in
        vddk) backend_vddk=ON ;;
        curl) backend_curl=ON ;;
        *) backend_auto=ON ;;
    esac
    BACKEND=$(radio "VDDK transport backend:" \
        auto "Automatic selection" "$backend_auto" \
        vddk "Use the VDDK library" "$backend_vddk" \
        curl "Use HTTP PUT/GET (no VDDK)" "$backend_curl") || abort

    # DISCARD_RUN_ID is the ID of a failed run to remove, not a flag ; blank
    # means a normal migration.
    discard_default=no
    [ -n "${DISCARD_RUN_ID:-}" ] && discard_default=yes
    if yesno "Discard a failed run instead of migrating?\n\nThis removes that run's snapshot and staged data. Only answer Yes if you want to give up on a failed run." "$discard_default" ; then
        while : ; do
            DISCARD_RUN_ID=$(input "Run ID to discard (digits, from the failed run's output):" "${DISCARD_RUN_ID:-}") || abort
            [[ $DISCARD_RUN_ID =~ ^[0-9]{1,20}$ ]] && break
            bad "The run ID must be digits only." || true
        done
    else
        DISCARD_RUN_ID=""
    fi

    ca_default=no
    [ -n "${VCENTER_CA_BUNDLE:-}" ] && ca_default=yes
    if yesno "Provide a custom CA bundle for the vCenter certificate?\n\nSay Yes if vCenter uses a certificate not trusted by the system CA store." "$ca_default" ; then
        while : ; do
            VCENTER_CA_BUNDLE=$(input "Path to the CA bundle PEM file:" "${VCENTER_CA_BUNDLE:-}") || abort
            if [ -r "$(anchor_path "$VCENTER_CA_BUNDLE")" ] ; then
                # The playbook runs from the repository, so anchor the path.
                VCENTER_CA_BUNDLE=$(realpath -- "$(anchor_path "$VCENTER_CA_BUNDLE")")
                break
            fi
            bad "That file is not readable: $VCENTER_CA_BUNDLE" || true
        done
    else
        VCENTER_CA_BUNDLE=""
    fi

    # Only the OpenStack wrapper converts to QCOW2 ; Azure writes fixed VHDs.
    if [ "$MIGRATE_WRAPPER" = migrate-openstack-vm-vddk.sh ] ; then
        compress_default=no
        [ "${COMPRESS:-false}" = true ] && compress_default=yes
        if yesno "Compress the QCOW2 images before uploading them?\n\nThis can reduce upload size on a slow link but uses more CPU." "$compress_default" ; then
            COMPRESS=true
        else
            COMPRESS=false
        fi

        guest_default=no
        [ "${GUEST_CONVERT:-false}" = true ] && guest_default=yes
        if yesno "Convert the guest for KVM with virt-v2v-in-place?\n\nInstalls virtio drivers and removes VMware tools. Required for Windows. Stages every disk before upload." "$guest_default" ; then
            GUEST_CONVERT=true
            # Windows guests also need the virtio-win drivers; leave empty for
            # Linux guests or when VIRTIO_WIN / the default location applies.
            VIRTIO_WIN=$(input "virtio-win ISO or directory (blank: download stable ISO to ${WEB_INPUT_DIR:-${ARTIFACT_ROOT:-/work/migrate-v2v}/web-inputs}/virtio-win.iso):" "${VIRTIO_WIN:-}") || abort
            [ -z "$VIRTIO_WIN" ] || VIRTIO_WIN=$(realpath -m -- "$(anchor_path "$VIRTIO_WIN")")
        else
            GUEST_CONVERT=false
            VIRTIO_WIN=""
        fi
    fi
fi

method=""
case "$MIGRATE_WRAPPER" in *-vddk.sh) method=" (datastore-direct)" ;; esac
summary="Migration summary for $VM_NAME$method

  Source vCenter..: ${VSPHERE_TARGET_VCENTER:-${VCENTER_DATACENTER:-auto-select}}
  Target..........: ${!MIGRATE_TARGET_VAR:-auto-select}
  Validate certs..: ${VALIDATE_CERTS:-vault default}
  Artifact root...: $ARTIFACT_ROOT
  Quiesce snapshot: $QUIESCE_SNAPSHOT
  Preflight only..: $PREFLIGHT_ONLY"
[ "$MIGRATE_WRAPPER" != migrate-openstack-vm-vddk.sh ] || summary="$summary
  Compress images.: $COMPRESS
  Guest convert...: ${GUEST_CONVERT:-false}"
[[ $MIGRATE_WRAPPER == *-vddk.sh ]] && summary="$summary
  Require CBT.....: ${REQUIRE_CBT:-false}
  Backend.........: $BACKEND
  Discard run.....: ${DISCARD_RUN_ID:-none}
  CA bundle.......: ${VCENTER_CA_BUNDLE:-none}"
say "$summary" 16

file=$(mktemp "${TMPDIR:-/tmp}/${DIALOG_NAME}.XXXXXX")
dialog_tmp_files+=("$file")
{
    printf '#!/bin/bash\n'
    emit_runtime_path_exports
    printf 'export ARTIFACT_ROOT=%s\n' "$(quote "$ARTIFACT_ROOT")"
    printf 'export MIGRATE_V2V_VAULT_DIR=%s\n' "$(quote "$MIGRATE_V2V_VAULT_DIR")"
    printf 'export MIGRATE_V2V_CATALOG_DIR=%s\n' "$(quote "$MIGRATE_V2V_CATALOG_DIR")"
    printf 'export VM_NAME=%s\n' "$(quote "$VM_NAME")"
    printf 'export VCENTER_DATACENTER=%s\n' "$(quote "$VCENTER_DATACENTER")"
    printf 'export VSPHERE_TARGET_VCENTER=%s\n' "$(quote "$VSPHERE_TARGET_VCENTER")"
    printf 'export %s=%s\n' "$MIGRATE_TARGET_VAR" "$(quote "$target_value")"
    printf 'export VALIDATE_CERTS=%s\n' "$(quote "$VALIDATE_CERTS")"
    printf 'export QUIESCE_SNAPSHOT=%s\n' "$(quote "$QUIESCE_SNAPSHOT")"
    printf 'export PREFLIGHT_ONLY=%s\n' "$(quote "$PREFLIGHT_ONLY")"
    [ "$MIGRATE_WRAPPER" != migrate-openstack-vm-vddk.sh ] || {
        printf 'export COMPRESS=%s\n' "$(quote "$COMPRESS")"
        printf 'export GUEST_CONVERT=%s\n' "$(quote "${GUEST_CONVERT:-false}")"
        printf 'export VIRTIO_WIN=%s\n' "$(quote "${VIRTIO_WIN:-}")"
    }
    [[ $MIGRATE_WRAPPER == *-vddk.sh ]] && {
        printf 'export REQUIRE_CBT=%s\n' "$(quote "$REQUIRE_CBT")"
        printf 'export BACKEND=%s\n' "$(quote "$BACKEND")"
        if [ -n "$DISCARD_RUN_ID" ] ; then
            printf 'export DISCARD_RUN_ID=%s\n' "$(quote "$DISCARD_RUN_ID")"
        else
            printf 'unset DISCARD_RUN_ID\n'
        fi
        if [ -n "$VCENTER_CA_BUNDLE" ] ; then
            printf 'export VCENTER_CA_BUNDLE=%s\n' "$(quote "$VCENTER_CA_BUNDLE")"
        else
            printf 'unset VCENTER_CA_BUNDLE\n'
        fi
    }
    printf '%s\n' "$(quote "$script_dir/$MIGRATE_WRAPPER")"
} > "$file"
chmod +x "$file"
finish "$file" "$VM_NAME" migrate
