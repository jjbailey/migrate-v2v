#!/bin/bash
# examples/controller-v2v.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# First-time host setup for the Ansible controller and LAN web site.
# Installs Caddy from its official Ubuntu/Debian package repository if missing.
# Encrypted vaults must already be under ARTIFACT_ROOT/vaults.
#
# Usage: VAULT_PASSWORD=... examples/controller-v2v.sh   (from any directory)
#   (if VAULT_PASSWORD is unset and stdin is a terminal, you are prompted)
# refuses to overwrite ~/.dib-vault-pass or the generated systemd units;
# set FORCE=1 to overwrite them and reset the operator web account.
#
# This installs the pinned Python dependencies and Ansible collections in
# ~/.dib7, prepares ARTIFACT_ROOT (default /work/migrate-v2v), creates the
# operator web account, and starts the web app and Caddy as systemd services.

set -Eeuo pipefail

die()
{
    echo "ERROR: $*" >&2
    exit 1
}

# run from the repository root, wherever the checkout lives and wherever the
# script is invoked from
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." || die "cannot find the repository root"
PROJECT_HOME=$(pwd -P)

[[ $(id -u) -ne 0 ]] || die "run this script as the account that will own the controller, not as root"

for cmd in apt-get awk find getent id ip mktemp python3 ss sudo systemctl ; do
    command -v "$cmd" > /dev/null || die "required command not found: $cmd"
done
[[ -d /run/systemd/system ]] || die "systemd is not running on this host"
[[ -f requirements.txt && -f requirements.yml && -f web/app.py && -f web/Caddyfile ]] ||
    die "required project files not found in $PROJECT_HOME"
if ss -H -ltn 'sport = :8443' | awk 'END {exit NR == 0}' ; then
    die "TCP port 8443 is already in use; stop or reconfigure the current listener before installing this controller"
fi

sudo -v || die "sudo access is required to install python3-venv, create the artifact root, and install systemd units"

SYSTEM_UID=$(id -u)
SYSTEM_GID=$(id -g)
SYSTEM_USER=$(id -un)
USER_HOME=$(getent passwd "$SYSTEM_USER" | awk -F: '{print $6}')
[[ -n $USER_HOME && -d $USER_HOME ]] || die "could not find the home directory for $SYSTEM_USER"
VENV_DIR=$USER_HOME/.dib7
VAULT_PASS_FILE=$USER_HOME/.dib-vault-pass
ARTIFACT_ROOT=${ARTIFACT_ROOT:-/work/migrate-v2v}
case "$ARTIFACT_ROOT" in
    /*) ;;
    *) ARTIFACT_ROOT="$PROJECT_HOME/$ARTIFACT_ROOT" ;;
esac
WEB_STATE_DIR=$ARTIFACT_ROOT/web-state
WEB_UNIT=/etc/systemd/system/migrate-v2v-web.service
CADDY_UNIT=/etc/systemd/system/migrate-v2v-caddy.service

if [[ ${FORCE:-} != 1 ]] ; then
    [[ ! -e $VAULT_PASS_FILE ]] || die "$VAULT_PASS_FILE already exists; remove it or rerun with FORCE=1 to overwrite"
    [[ ! -e $WEB_UNIT ]] || die "$WEB_UNIT already exists; remove it or rerun with FORCE=1 to overwrite"
    [[ ! -e $CADDY_UNIT ]] || die "$CADDY_UNIT already exists; remove it or rerun with FORCE=1 to overwrite"
elif sudo systemctl is-active --quiet migrate-v2v-web.service ||
    sudo systemctl is-active --quiet migrate-v2v-caddy.service ; then
    die "stop the controller services before rerunning with FORCE=1; restarting the web service can interrupt an active migration"
fi

# A caller may run this script with bash -x. Do not trace any operation that
# reads, checks, or writes the vault password.
TRACE_WAS_ON=0
case $- in
    *x*) TRACE_WAS_ON=1; set +x ;;
esac

# Ask for the vault password before installing packages or writing files.
if [[ -z ${VAULT_PASSWORD:-} ]] ; then
    if [[ -t 0 ]] ; then
        read -r -s -p "Vault password: " VAULT_PASSWORD
        echo
    fi
    [[ -n ${VAULT_PASSWORD:-} ]] || die "VAULT_PASSWORD is empty or unset"
fi

# This project documents Ubuntu 24.04 as its host baseline. python3-venv is
# needed because Ubuntu marks the system Python as externally managed.
sudo apt-get update
sudo apt-get install -y python3-venv

if ! command -v caddy > /dev/null 2>&1 ; then
    # Follow Caddy's official stable Debian/Ubuntu package instructions.
    sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
        sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' |
        sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
    sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update
    sudo apt-get install -y caddy
    # The package enables its generic caddy.service. This setup uses the
    # separately generated migrate-v2v-caddy.service and its own Caddyfile.
    sudo systemctl disable --now caddy.service
fi

(
    umask 077
    printf '%s\n' "$VAULT_PASSWORD" > "$VAULT_PASS_FILE"
)
unset VAULT_PASSWORD
chmod 600 "$VAULT_PASS_FILE"
if [[ $TRACE_WAS_ON == 1 ]] ; then
    set -x
fi

sudo install -d -o "$SYSTEM_UID" -g "$SYSTEM_GID" -m 0750 "$ARTIFACT_ROOT"
mkdir -p "$ARTIFACT_ROOT/vaults"
if [[ -z $(find "$ARTIFACT_ROOT/vaults" -mindepth 1 -maxdepth 1 -print -quit) ]] ; then
    echo "WARNING: $ARTIFACT_ROOT/vaults is empty; copy the encrypted vaults there" >&2
fi

python3 -m venv "$VENV_DIR"
export DIB7_VENV_BIN=$VENV_DIR/bin
export PATH="$DIB7_VENV_BIN:$PATH"
python -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml

# Install the Azure collection's Python SDK dependencies into the same
# environment as ansible-core, matching the host setup in README.md.
AZURE_REQUIREMENTS=$USER_HOME/.ansible/collections/ansible_collections/azure/azcollection/requirements.txt
[[ -f $AZURE_REQUIREMENTS ]] || die "Azure collection requirements not found after collection install: $AZURE_REQUIREMENTS"
python -m pip install -r "$AZURE_REQUIREMENTS" -c requirements-azure-constraints.txt

WEB_ADDRESS=${WEB_ADDRESS:-$(ip -4 route get 1.1.1.1 2> /dev/null |
    awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}' || true)}
[[ -n $WEB_ADDRESS ]] || die "could not determine this host's IPv4 address; set WEB_ADDRESS and rerun"
python - "$WEB_ADDRESS" << 'PY' || die "WEB_ADDRESS must be a valid, non-loopback IPv4 address"
import ipaddress
import sys
address = ipaddress.ip_address(sys.argv[1])
if address.version != 4 or address.is_loopback:
    raise SystemExit(1)
PY
if ! ip -4 -o addr show | awk -v wanted="$WEB_ADDRESS" '{split($4, cidr, "/"); if (cidr[1] == wanted) found = 1} END {exit !found}' ; then
    die "WEB_ADDRESS=$WEB_ADDRESS is not assigned to this host"
fi

export ARTIFACT_ROOT WEB_STATE_DIR
export VAULT_PASSWORD_FILE=$VAULT_PASS_FILE
"$VENV_DIR/bin/python" web/app.py user add operator

# systemd quoted values need backslashes and double quotes escaped.
systemd_quote()
{
    local value=${1//\\/\\\\}
    value=${value//\"/\\\"}
    printf '"%s"' "$value"
}

# WorkingDirectory= does not use shell-style quotes. Escape control characters,
# spaces, quotes, backslashes, and percent specifiers as unit-file syntax.
systemd_path_value()
{
    python3 -c '
import os
import sys

path = sys.argv[1]
if not os.path.isabs(path):
    raise SystemExit("systemd path must be absolute")
parts = []
for byte in os.fsencode(path):
    if byte == 0x25:
        parts.append("%%")
    elif 0x21 <= byte <= 0x7e and byte not in (0x22, 0x5c):
        parts.append(chr(byte))
    else:
        parts.append(f"\\x{byte:02x}")
print("".join(parts), end="")
' "$1"
}

CADDY_BIN=$(command -v caddy)
HOST_PATH="$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin"
CADDY_HOSTS="localhost:8443, 127.0.0.1:8443, [::1]:8443, $WEB_ADDRESS:8443"
WEB_UNIT_TMP=$(mktemp)
CADDY_UNIT_TMP=$(mktemp)
cleanup()
{
    rm -f "$WEB_UNIT_TMP" "$CADDY_UNIT_TMP"
}
trap cleanup EXIT

{
    cat << EOF
[Unit]
Description=migrate-v2v controller web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SYSTEM_USER
Group=$SYSTEM_GID
WorkingDirectory=$(systemd_path_value "$PROJECT_HOME")
Environment=$(systemd_quote "HOME=$USER_HOME")
Environment=$(systemd_quote "PATH=$HOST_PATH")
Environment=$(systemd_quote "ARTIFACT_ROOT=$ARTIFACT_ROOT")
Environment=$(systemd_quote "WEB_STATE_DIR=$WEB_STATE_DIR")
Environment=$(systemd_quote "VAULT_PASSWORD_FILE=$VAULT_PASS_FILE")
Environment="OVFTOOL_ACCEPT_LICENSE=1"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/usr/bin/env python3 web/app.py serve --host 127.0.0.1 --port 8080
KillMode=mixed
TimeoutStopSec=30s
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
UMask=0022

[Install]
WantedBy=multi-user.target
EOF
} > "$WEB_UNIT_TMP"

{
    cat << EOF
[Unit]
Description=Caddy for migrate-v2v controller
After=network-online.target migrate-v2v-web.service
Wants=network-online.target
Requires=migrate-v2v-web.service

[Service]
Type=simple
User=$SYSTEM_USER
Group=$SYSTEM_GID
WorkingDirectory=$(systemd_path_value "$PROJECT_HOME")
Environment=$(systemd_quote "HOME=$USER_HOME")
Environment=$(systemd_quote "CADDY_HOSTS=$CADDY_HOSTS")
Environment=$(systemd_quote "CADDY_DEFAULT_SNI=$WEB_ADDRESS")
ExecStart=$(systemd_quote "$CADDY_BIN") run --config $(systemd_quote "$PROJECT_HOME/web/Caddyfile")
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
UMask=0022

[Install]
WantedBy=multi-user.target
EOF
} > "$CADDY_UNIT_TMP"

sudo install -o root -g root -m 0644 "$WEB_UNIT_TMP" "$WEB_UNIT"
sudo install -o root -g root -m 0644 "$CADDY_UNIT_TMP" "$CADDY_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable migrate-v2v-web.service migrate-v2v-caddy.service
sudo systemctl restart migrate-v2v-web.service migrate-v2v-caddy.service

echo "Done. Open https://$WEB_ADDRESS:8443 (also https://localhost:8443 on this host)."
echo "Caddy's local root certificate: $USER_HOME/.local/share/caddy/pki/authorities/local/root.crt"
echo "Trust that certificate on each client, and allow TCP port 8443 through the host firewall for LAN access."
echo "Host-specific migration tools are documented in README.md under Prepare the Control Host."
