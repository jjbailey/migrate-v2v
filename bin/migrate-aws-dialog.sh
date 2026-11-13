#!/bin/bash
# bin/migrate-aws-dialog.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# shellcheck disable=SC2034 # configuration read by migrate-dialog-common.sh
# Dialog front end for bin/migrate-aws-vm.sh.

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
orig_pwd=$PWD
cd "$script_dir/.." || exit
# shellcheck disable=SC2034 # read by migrate-dialog-common.sh
DIALOG_NAME=migrate-aws-dialog
# shellcheck disable=SC2034 # read by launch-dialog-common.sh
ACTION_VERB=Migration
MIGRATE_TARGET_VAR=AWS_TARGET_PROJECT
MIGRATE_TARGET_LABEL='AWS account'
MIGRATE_VAULT=aws.yml
MIGRATE_VAULT_MAP=aws_projects
MIGRATE_WRAPPER=migrate-aws-vm.sh
# shellcheck source=migrate-dialog-common.sh
source "$script_dir/migrate-dialog-common.sh"
