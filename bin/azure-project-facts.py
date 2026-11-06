#!/usr/bin/env python3
# bin/azure-project-facts.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""List the SSH public key resources one Azure subscription can launch with.

Credentials come from vaults/azure.yml (azure_projects, matched by vault key
or subscription ID, the same way select-project.yml does). The service
principal in the vault entry is always used, never a logged-in az CLI
session, so the listing sees what the launch playbook will see.

With --keypairs (the default and, for now, the only listing), the SSH public
key resources in the vault entry's resource group are listed: name, key
type, SHA256 fingerprint (as ssh-keygen -l prints it), and location. These
are what launch-azure-instance.yml resolves AZURE_SSH_KEY_NAME against, the
way the AWS and OpenStack launches use a registered key pair.
--resource-group lists another group instead.

Output is a table by default, or JSON with --json for scripting
(launch-azure-dialog.sh uses this for its key picker).
"""

import argparse
import base64
import binascii
import hashlib
import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vault_common import read_vault  # noqa: E402

REQUIRED_KEYS = ("tenant_id", "subscription_id", "client_id",
                 "client_secret", "resource_group")

def select_vault_entry(vault, requested):
    data = read_vault(vault)
    projects = {str(key): value or {} for key, value in (
        data.get("azure_projects") or {}).items()}
    if requested:
        for key, entry in projects.items():
            if requested in {key, str(entry.get("subscription_id", ""))}:
                return key, entry
        raise SystemExit(
            f"Azure project '{requested}' matches no vault key or "
            f"subscription ID in {vault}. "
            f"Available: {', '.join(projects) or '<none>'}"
        )
    if len(projects) == 1:
        return next(iter(projects.items()))
    raise SystemExit(
        f"{vault} holds {'no' if not projects else 'several'} projects; "
        f"name one as the PROJECT argument ({', '.join(projects) or '<none>'})"
    )

def require_entry(key, entry):
    missing = [name for name in REQUIRED_KEYS
               if not str(entry.get(name, "") or "").strip()]
    if missing:
        raise SystemExit(
            f"vault entry '{key}' is missing {', '.join(missing)}")

def key_fingerprint(public_key):
    """Return (type, SHA256:...) for an OpenSSH public key line.

    The fingerprint matches `ssh-keygen -lf`, so a listed key can be compared
    with a local .pub file. A key that does not parse gives empty strings.
    """
    parts = str(public_key or "").split()
    if len(parts) < 2:
        return "", ""
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return parts[0], ""
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode()
    return parts[0], "SHA256:" + digest.rstrip("=")

def gather_keypairs(client, resource_group):
    pairs = []
    for key in client.ssh_public_keys.list_by_resource_group(resource_group):
        key_type, fingerprint = key_fingerprint(key.public_key)
        pairs.append({
            "name": key.name or "",
            "type": key_type,
            "fingerprint": fingerprint,
            "location": key.location or "",
            "id": key.id or "",
        })
    pairs.sort(key=lambda pair: pair["name"])
    return pairs

def print_keypairs_table(pairs, resource_group):
    print(f"SSH public keys (resource group {resource_group}): {len(pairs)}")
    if not pairs:
        return
    widths = {"name": 4, "type": 4, "fp": 11}
    for pair in pairs:
        widths["name"] = max(widths["name"], len(pair["name"]))
        widths["type"] = max(widths["type"], len(pair["type"]))
        widths["fp"] = max(widths["fp"], len(pair["fingerprint"]))
    print(f"  {'NAME':<{widths['name']}}  {'TYPE':<{widths['type']}}  "
          f"{'FINGERPRINT':<{widths['fp']}}  LOCATION")
    for pair in pairs:
        print(f"  {pair['name']:<{widths['name']}}  "
              f"{pair['type']:<{widths['type']}}  "
              f"{pair['fingerprint']:<{widths['fp']}}  "
              f"{pair['location']}")

def make_client(entry):
    try:
        from azure.identity import ClientSecretCredential
        from azure.mgmt.compute import ComputeManagementClient
    except ImportError:
        raise SystemExit(
            "azure-identity and azure-mgmt-compute are required (see README)")
    credential = ClientSecretCredential(
        str(entry["tenant_id"]), str(entry["client_id"]),
        str(entry["client_secret"]))
    return ComputeManagementClient(credential, str(entry["subscription_id"]))

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", nargs="?",
                        help="vault key or subscription ID "
                             "(omit for a single-entry vault)")
    parser.add_argument("--vault", default=str(REPO_DIR / "vaults/azure.yml"))
    parser.add_argument("--keypairs", dest="list_keypairs", action="store_true",
                        help="list SSH public key resources (the default)")
    parser.add_argument("--resource-group",
                        help="override the vault entry's resource_group")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="machine-readable output for scripting")
    args = parser.parse_args(argv)

    key, entry = select_vault_entry(args.vault, args.project)
    require_entry(key, entry)
    resource_group = (args.resource_group
                      or str(entry["resource_group"]).strip())
    client = make_client(entry)

    from azure.core.exceptions import AzureError
    try:
        pairs = gather_keypairs(client, resource_group)
    except AzureError as error:
        raise SystemExit(f"could not list SSH public keys: {error}")

    if args.as_json:
        json.dump({"project": key,
                   "subscription_id": str(entry["subscription_id"]),
                   "resource_group": resource_group,
                   "keypairs": pairs},
                  sys.stdout, indent=2, default=str)
        print()
        return 0
    print(f"Azure project: {key}, subscription {entry['subscription_id']}")
    print_keypairs_table(pairs, resource_group)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"azure-project-facts: {error}")
