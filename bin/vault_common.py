#!/usr/bin/env python3
# bin/vault_common.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Shared Ansible Vault command and decryption helpers for project tools."""

import os
import shutil
import subprocess
from pathlib import Path

import yaml

DEFAULT_VAULT_DIR = Path("/work/migrate-v2v/vaults")

def default_vault_path(filename):
    """Runtime vault path, overridable for alternate checkouts or tests."""
    vault_dir = Path(
        os.environ.get("MIGRATE_V2V_VAULT_DIR") or DEFAULT_VAULT_DIR
    ).expanduser()
    return vault_dir / filename

def ansible_vault_bin():
    """Find ansible-vault in the configured virtualenv or on PATH."""
    venv_bin = Path(os.environ.get("DIB7_VENV_BIN", Path.home() / ".dib7/bin"))
    candidate = venv_bin / "ansible-vault"
    if os.access(candidate, os.X_OK):
        return str(candidate)
    found = shutil.which("ansible-vault")
    if not found:
        raise SystemExit(
            "ansible-vault not found in $DIB7_VENV_BIN or on PATH")
    return found

def read_vault(vault):
    """Decrypt a vault, using the configured or default password file."""
    command = [ansible_vault_bin(), "view", str(vault)]
    # ~/.ssh/dib-vault-pass is the old default location, kept as a fallback.
    for passfile in (os.environ.get("VAULT_PASSWORD_FILE"),
                     str(Path.home() / ".dib-vault-pass"),
                     str(Path.home() / ".ssh/dib-vault-pass")):
        if passfile and Path(passfile).is_file() and Path(passfile).stat().st_size:
            command += ["--vault-password-file", passfile]
            break
    result = subprocess.run(
        command, stdout=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"could not decrypt {vault}")
    return yaml.safe_load(result.stdout) or {}

def vault_projects(vault, map_name):
    """The vault's <map_name> entries keyed by string, or None without a file.

    The bin/ tools take credentials in this order: an explicit command-line
    flag, then credential variables exported in the environment, then the
    vault, then cached logins (az login, ~/.aws, gcloud). Callers decrypt
    the vault only when no flag or variable decided, so a user working from
    exported credentials is never prompted for the vault password. A vault
    that exists but cannot be decrypted stops the run instead of falling
    through to a cached login.
    """
    path = Path(vault)
    if not path.exists():
        return None
    data = read_vault(path) or {}
    return {str(key): value or {} for key, value in (
        data.get(map_name) or {}).items()}

def match_project(projects, wanted, fields):
    """First (key, entry) whose key or one of fields equals a wanted value.

    wanted is one identity or an iterable of them; comparisons are on
    strings, case-insensitively. Returns (None, None) when nothing matches.
    """
    if isinstance(wanted, str):
        wanted = {wanted}
    wanted = {str(value).lower() for value in wanted or () if str(value)}
    for key, entry in (projects or {}).items():
        identities = {key} | {str(entry.get(field, "") or "") for field in fields}
        if {identity.lower() for identity in identities if identity} & wanted:
            return key, entry
    return None, None

def environment_source(label, vault):
    """Describe an environment credential source, noting a skipped vault."""
    if Path(vault).exists():
        return f"{label}; {Path(vault).name} not consulted"
    return label
