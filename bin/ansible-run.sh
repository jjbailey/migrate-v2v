#!/bin/bash
# bin/ansible-run.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Wrapper to run ansible-playbook with the vault password wired up.
#
# All credentials live in the encrypted files under
# /work/migrate-v2v/vaults/ by default, so ansible needs
# the vault password to load them. This wrapper
# appends one of, in order:
#   1. nothing, if you already passed --ask-vault-pass, --vault-password-file,
#      or --vault-id
#   2. --vault-password-file $VAULT_PASSWORD_FILE, if that env var points at
#      a non-empty file (the value is a path, not the secret itself)
#   3. --vault-password-file ~/.dib-vault-pass, if that file exists
#   4. --vault-password-file ~/.ssh/dib-vault-pass, the old default location,
#      with a warning to move it
#   5. --ask-vault-pass (interactive prompt)
set -euo pipefail

script_dir=$(CDPATH='' builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")" > /dev/null && builtin pwd -P)

usage()
{
    cat << EOF
Usage: ansible-run playbook.yml [ansible-playbook args...]

Appends a vault-password option unless one was already given, then execs
ansible-playbook.
EOF
    exit 2
}

if [ "$#" -lt 1 ] ; then
    usage >&2
fi

# Standard migrations export through OVF Tool ; the datastore-direct VDDK
# playbooks, launches, and stage-v2v do not need it.
playbook_name=${1##*/}
case "$playbook_name" in
    migrate-aws-vm.yml | migrate-azure-vm.yml | migrate-gcp-vm.yml | migrate-openstack-vm.yml)
        OVFTOOL_INSTALL_DIR=${OVFTOOL_INSTALL_DIR:-/work/migrate-v2v/tools/ovftool-5.1.0}
        case "$OVFTOOL_INSTALL_DIR" in
            /*) ;;
            *) OVFTOOL_INSTALL_DIR="$PWD/$OVFTOOL_INSTALL_DIR" ;;
        esac
        export OVFTOOL_INSTALL_DIR
        if ! command -v ovftool > /dev/null 2>&1 ; then
            if [ ! -x "$OVFTOOL_INSTALL_DIR/ovftool" ] ; then
                "$script_dir/install-ovftool.py"
            fi
            PATH="$OVFTOOL_INSTALL_DIR:$PATH"
            export PATH
            if ! command -v ovftool > /dev/null 2>&1 ; then
                echo "ansible-run: OVF Tool is still unavailable after installation." >&2
                exit 1
            fi
        fi
        ;;
esac

DEFAULT_VAULT_PASSWORD_FILE="$HOME/.dib-vault-pass"
LEGACY_VAULT_PASSWORD_FILE="$HOME/.ssh/dib-vault-pass"

ARGS=("$@")
has_vault_option=false
for arg in "${ARGS[@]}" ; do
    case "$arg" in
        --ask-vault-pass | --ask-vault-password | -J | \
            --vault-password-file | --vault-password-file=* | \
            --vault-pass-file | --vault-pass-file=* | \
            --vault-id | --vault-id=*)
            has_vault_option=true
            break
            ;;
    esac
done

if [ "$has_vault_option" = true ] ; then
    : # vault password handling already on the command line
elif [ -n "${VAULT_PASSWORD_FILE:-}" ] && [ -s "${VAULT_PASSWORD_FILE}" ] ; then
    ARGS+=("--vault-password-file" "${VAULT_PASSWORD_FILE}")
elif [ -s "${DEFAULT_VAULT_PASSWORD_FILE}" ] ; then
    ARGS+=("--vault-password-file" "${DEFAULT_VAULT_PASSWORD_FILE}")
elif [ -s "${LEGACY_VAULT_PASSWORD_FILE}" ] ; then
    echo "ansible-run: using the old vault password location ${LEGACY_VAULT_PASSWORD_FILE};" \
        "move it to ${DEFAULT_VAULT_PASSWORD_FILE}" >&2
    ARGS+=("--vault-password-file" "${LEGACY_VAULT_PASSWORD_FILE}")
else
    ARGS+=("--ask-vault-pass")
fi

exec ansible-playbook "${ARGS[@]}"
