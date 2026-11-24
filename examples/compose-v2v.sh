#!/bin/bash
# examples/compose-v2v.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# this is an example script for first-time docker web site setup.
# have a vault password ready.
# have a password for the operator ready.
#
# usage: VAULT_PASSWORD=... examples/compose-v2v.sh   (from any directory)
#   (if VAULT_PASSWORD is unset and stdin is a terminal, you are prompted)
# refuses to overwrite ~/.dib-vault-pass or web/.env if they exist;
# set FORCE=1 to overwrite them.
#
# this is a LAN install: it detects the host IPv4 address, sets WEB_ADDRESS to
# it, and always uses web/compose.lan.yaml.
# it creates the artifact root (ARTIFACT_ROOT, default /work/migrate-v2v)
# owned by the invoking user and sets HOST_UID/HOST_GID from that user.
# the encrypted vaults must already be in $ARTIFACT_ROOT/vaults.

set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

# run from the repository root, wherever the checkout lives and wherever the
# script is invoked from
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." || die "cannot find the repository root"

for cmd in docker ip awk sed sudo; do
    command -v "$cmd" > /dev/null || die "required command not found: $cmd"
done
[ -f web/.env.example ] || die "web/.env.example not found in $PWD"

# the vault password must be set and non-empty; prompt if we can, else fail
if [ -z "${VAULT_PASSWORD:-}" ]; then
    if [ -t 0 ]; then
        read -r -s -p "Vault password: " VAULT_PASSWORD
        echo
    fi
    [ -n "${VAULT_PASSWORD:-}" ] || die "VAULT_PASSWORD is empty or unset"
fi

# write the password file privately from the start (no window where it is
# world-readable); printf avoids echo's option/escape handling
VAULT_PASS_FILE=$HOME/.dib-vault-pass
if [ "${FORCE:-}" != 1 ]; then
    for f in "$VAULT_PASS_FILE" web/.env; do
        [ ! -e "$f" ] || die "$f already exists; remove it or rerun with FORCE=1 to overwrite"
    done
fi
(umask 077; printf '%s\n' "$VAULT_PASSWORD" > "$VAULT_PASS_FILE")
chmod 600 "$VAULT_PASS_FILE"

ARTIFACT_ROOT=${ARTIFACT_ROOT:-/work/migrate-v2v}
sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0750 "$ARTIFACT_ROOT"
mkdir -p "$ARTIFACT_ROOT/vaults"
[ -n "$(ls -A "$ARTIFACT_ROOT/vaults")" ] ||
    echo "WARNING: $ARTIFACT_ROOT/vaults is empty; copy the encrypted vaults there" >&2

sed -e "s,^\(VAULT_PASSWORD_FILE_HOST\)=.*,\1=$VAULT_PASS_FILE," \
    -e "s,^\(ARTIFACT_ROOT_HOST\)=.*,\1=$ARTIFACT_ROOT," \
    -e "s/^\(HOST_UID\)=.*/\1=$(id -u)/" \
    -e "s/^\(HOST_GID\)=.*/\1=$(id -g)/" \
    web/.env.example > web/.env
chmod 600 web/.env

WEB_ADDRESS=$(ip -4 route get 1.1.1.1 2> /dev/null |
    awk '{for (i=1 ; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}') || true
[ -n "$WEB_ADDRESS" ] || die "could not determine this host's IPv4 address; set WEB_ADDRESS in web/.env by hand"
sed -i "s/^\(WEB_ADDRESS\)=.*/\1=$WEB_ADDRESS/" web/.env

compose=(sudo docker compose --env-file web/.env -f web/compose.yaml)
compose_lan=("${compose[@]}" -f web/compose.lan.yaml)

"${compose[@]}" build controller

"${compose_lan[@]}" run --rm --no-deps controller python3 web/app.py user add operator

"${compose_lan[@]}" up -d

echo "Done. WEB_ADDRESS=$WEB_ADDRESS (see web/.env)"
