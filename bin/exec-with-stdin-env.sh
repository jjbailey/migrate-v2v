#!/bin/bash
# bin/exec-with-stdin-env.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Usage: exec-with-stdin-env.sh NAME... -- COMMAND [ARG...]
#
# Reads one line from stdin per NAME, exports each non-empty value under that
# name, then execs COMMAND. Playbooks use it to hand a secret to a CLI (govc,
# aws) through the command module's stdin: an Ansible `environment:` would run
# the module as `sh -c 'NAME=secret python ...'`, leaving the secret readable
# in ps by every local user for as long as the command runs.

set -euo pipefail

names=()
while [ $# -gt 0 ] && [ "$1" != "--" ] ; do
    if [[ ! $1 =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] ; then
        printf 'exec-with-stdin-env.sh: invalid variable name: %s\n' "$1" >&2
        exit 2
    fi
    names+=("$1")
    shift
done
if [ $# -lt 2 ] || [ ${#names[@]} -eq 0 ] ; then
    echo "usage: exec-with-stdin-env.sh NAME... -- COMMAND [ARG...]" >&2
    exit 2
fi
shift

for name in "${names[@]}" ; do
    value=
    IFS= read -r value || true
    if [ -n "$value" ] ; then
        export "$name=$value"
    fi
done

exec "$@"
