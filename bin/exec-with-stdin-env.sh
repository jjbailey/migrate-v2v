#!/bin/bash
# bin/exec-with-stdin-env.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Usage: exec-with-stdin-env.sh NAME... -- COMMAND [ARG...]
#
# Reads one line from stdin per NAME, exports each non-empty value under that
# name, then execs COMMAND. An empty line leaves NAME exactly as the caller's
# environment has it: the AWS upload relies on that when the vault holds no
# keys and the operator's exported credentials are the source. A caller that
# must drop an inherited value runs `env -u NAME` first (aws/import.yml does
# this for AWS_SESSION_TOKEN). A missing line is warned about and treated as
# empty. Playbooks use it to hand a secret to a CLI (govc,
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
    # read fails at EOF even when it returns a final line with no newline;
    # only warn when nothing at all was read for this name.
    if ! IFS= read -r value && [ -z "$value" ] ; then
        printf 'exec-with-stdin-env.sh: warning: stdin ended before a value for %s\n' "$name" >&2
    fi
    if [ -n "$value" ] ; then
        export "$name=$value"
    fi
done

exec "$@"
