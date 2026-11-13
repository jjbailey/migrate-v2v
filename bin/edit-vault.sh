#!/bin/bash
# bin/edit-vault.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Edit (or create) one encrypted vault file.
#
# The per-target entry points -- aws-vault.sh, azure-vault.sh, gcp-vault.sh,
# openstack-vault.sh, vsphere-vault.sh -- are symlinks to this
# script; the name it was invoked as selects
# $MIGRATE_V2V_VAULT_DIR/<target>.yml. Set VAULT to
# edit some other file.
#
# ansible-vault comes from $DIB7_VENV_BIN (default ~/.dib7/bin) if present,
# else PATH. The vault password is looked up in this order:
# $VAULT_PASSWORD_FILE, then ~/.dib-vault-pass, then the old
# ~/.ssh/dib-vault-pass, then a prompt.

set -eu

invoked_as="$(basename -- "$0")"
target="${invoked_as%-vault.sh}"
if [ "$target" = "edit" ] && [ -z "${VAULT:-}" ] ; then
    echo "$invoked_as: run this through <target>-vault.sh, or set VAULT" >&2
    exit 2
fi
if [ "$target" = "$invoked_as" ] || [ -z "$target" ] ; then
    if [ -z "${VAULT:-}" ] ; then
        echo "$invoked_as: run this through <target>-vault.sh, or set VAULT" >&2
        exit 2
    fi
    target=""
fi

VAULT="${VAULT:-${MIGRATE_V2V_VAULT_DIR:-/work/migrate-v2v/vaults}/$target.yml}"
# Prefer the dib7 venv's ansible-vault, else whatever is on PATH.
VENV_BIN="${DIB7_VENV_BIN:-$HOME/.dib7/bin}"
ANSIBLE_VAULT="$VENV_BIN/ansible-vault"
[ -x "$ANSIBLE_VAULT" ] || ANSIBLE_VAULT=$(command -v ansible-vault || true)

# A relative VAULT or MIGRATE_V2V_VAULT_DIR means the current directory, as
# it does for the Python tools; the playbooks need an absolute one.
case "$VAULT" in
    /*) ;;
    *) VAULT="$PWD/$VAULT" ;;
esac

[ -n "$ANSIBLE_VAULT" ] || { echo "ansible-vault not found in $VENV_BIN or on PATH" >&2; exit 1; }
umask 077
mkdir -p -- "$(dirname -- "$VAULT")"

# Vault password lookup: explicit file, default file, then prompt.
pass_args=()
if [ -n "${VAULT_PASSWORD_FILE:-}" ] && [ ! -s "$VAULT_PASSWORD_FILE" ] ; then
    echo "$invoked_as: warning: VAULT_PASSWORD_FILE=$VAULT_PASSWORD_FILE is missing or empty; trying the default password files" >&2
fi
if [ -n "${VAULT_PASSWORD_FILE:-}" ] && [ -s "$VAULT_PASSWORD_FILE" ] ; then
    pass_args=(--vault-password-file "$VAULT_PASSWORD_FILE")
elif [ -s "$HOME/.dib-vault-pass" ] ; then
    pass_args=(--vault-password-file "$HOME/.dib-vault-pass")
elif [ -s "$HOME/.ssh/dib-vault-pass" ] ; then
    # The old default location, kept as a fallback.
    pass_args=(--vault-password-file "$HOME/.ssh/dib-vault-pass")
fi

if [ -s "$VAULT" ] ; then
    action=edit
elif [ -e "$VAULT" ] ; then
    # An existing empty file (touched, or a pre-created bind-mount target):
    # create refuses it because it exists, and edit because it is not vault
    # data, so encrypt it in place first and then edit it.
    "$ANSIBLE_VAULT" encrypt --encrypt-vault-id=default "${pass_args[@]}" "$VAULT"
    action=edit
else
    action=create
fi

exec "$ANSIBLE_VAULT" "$action" --encrypt-vault-id=default "${pass_args[@]}" "$VAULT"
