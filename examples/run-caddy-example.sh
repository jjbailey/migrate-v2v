#!/bin/bash
# examples/run-caddy-example.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

set -Eeuo pipefail

# this script is an example for running caddy on the ansible controller
# for testing purposes.  modify as needed.

# users are in $ARTIFACT_ROOT/web-state/users.json
# to add users, e.g., operator
# python3 web/app.py user add operator

export PATH="$HOME/.dib7/bin:/usr/bin:/usr/local/bin"
PROJECT_HOME="$(CDPATH= builtin cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && builtin pwd -P)"
cd "$PROJECT_HOME"
umask 022

if ! command -v caddy > /dev/null 2>&1 ; then
    echo "run-caddy: Caddy is not installed or not on PATH." >&2
    echo "Install Caddy using the instructions at https://caddyserver.com/docs/install, then run this script again." >&2
    exit 127
fi

export VAULT_PASSWORD_FILE="${VAULT_PASSWORD_FILE:-$HOME/.dib-vault-pass}"

# Add each assigned IP as a Caddy site address so the wildcard listener can
# issue a matching local HTTPS certificate for every host interface.
declare -A CADDY_HOST_SEEN=()
CADDY_HOSTS=""
add_caddy_host()
{
    local host=$1
    [[ -n $host && -z ${CADDY_HOST_SEEN[$host]+set} ]] || return 0
    CADDY_HOST_SEEN[$host]=1
    CADDY_HOSTS="${CADDY_HOSTS:+$CADDY_HOSTS, }$host:8443"
}
add_caddy_host localhost
add_caddy_host 127.0.0.1
add_caddy_host '[::1]'
for address in $(hostname -I 2> /dev/null) ; do
    case "$address" in
        *:*) add_caddy_host "[$address]" ;;
        *) add_caddy_host "$address" ;;
    esac
done
export CADDY_HOSTS
# IP-literal clients omit SNI. Use the first discovered host address as the
# default TLS name; Caddy checks that such requests' Host header matches.
FIRST_CADDY_IPV4="$(hostname -I 2> /dev/null | awk '{for (i = 1; i <= NF; i++) if ($i !~ /:/) {print $i; exit}}')"
export CADDY_DEFAULT_SNI="${CADDY_DEFAULT_SNI:-${FIRST_CADDY_IPV4:-localhost}}"

caddy run --config "$PROJECT_HOME/web/Caddyfile" &
CADDY_PID=$!
python3 web/app.py serve &
APP_PID=$!

cleanup()
{
    local status=$?
    trap - EXIT INT TERM
    kill "$APP_PID" "$CADDY_PID" 2> /dev/null || true
    wait "$APP_PID" "$CADDY_PID" 2> /dev/null || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if wait -n "$APP_PID" "$CADDY_PID" ; then
    status=0
else
    status=$?
fi
exit "$status"
