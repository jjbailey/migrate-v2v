#!/bin/bash
# bin/migrate-azure-vddk-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# shellcheck disable=SC2034 # configuration read by migrate-dialog-common.sh
# Dialog front end for bin/migrate-azure-vm-vddk.sh.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by migrate-dialog-common.sh
DIALOG_NAME=migrate-azure-vddk-dialog
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
ACTION_VERB=Migration
MIGRATE_TARGET_VAR=AZURE_TARGET_PROJECT
MIGRATE_TARGET_LABEL='Azure subscription'
MIGRATE_VAULT=azure.yml
MIGRATE_VAULT_MAP=azure_projects
MIGRATE_WRAPPER=migrate-azure-vm-vddk.sh
# shellcheck source=migrate-dialog-common.sh
source "$script_dir/migrate-dialog-common.sh"
