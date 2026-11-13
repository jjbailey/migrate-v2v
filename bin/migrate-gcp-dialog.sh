#!/bin/bash
# bin/migrate-gcp-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# shellcheck disable=SC2034 # configuration read by migrate-dialog-common.sh
# Dialog front end for bin/migrate-gcp-vm.sh.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
orig_pwd=$PWD
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by migrate-dialog-common.sh
DIALOG_NAME=migrate-gcp-dialog
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
ACTION_VERB=Migration
MIGRATE_TARGET_VAR=GCP_TARGET_PROJECT
MIGRATE_TARGET_LABEL='GCP project'
MIGRATE_VAULT=gcp.yml
MIGRATE_VAULT_MAP=gcp_projects
MIGRATE_WRAPPER=migrate-gcp-vm.sh
# shellcheck source=migrate-dialog-common.sh
source "$script_dir/migrate-dialog-common.sh"
