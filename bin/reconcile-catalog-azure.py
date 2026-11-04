#!/usr/bin/env python3
# bin/reconcile-catalog-azure.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Remove Azure catalog entries when their managed images or snapshots are gone."""

import argparse
import json
import os
import re
from pathlib import Path

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.core.tools import parse_resource_id

from catalog_common import read_snapshot, remove_migration_entries
from vault_common import environment_source, match_project, vault_projects

# A managed image or snapshot resource ID, as azure_rm_image_info and
# azure_rm_snapshot_info report them.
RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+"
    r"/providers/Microsoft[.]Compute/(images|snapshots)/[^/]+$",
    re.IGNORECASE,
)

def credential_from_environment():
    """Prefer the AZURE_* names azure.azcollection reads, then the SDK's own.

    AZURE_TENANT or AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_SECRET or
    AZURE_CLIENT_SECRET. EnvironmentCredential spells the tenant and secret
    differently, so accept either. Returns None unless all three are set.
    """
    tenant = os.environ.get("AZURE_TENANT") or os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("AZURE_CLIENT_ID")
    secret = os.environ.get("AZURE_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
    if tenant and client_id and secret:
        return ClientSecretCredential(tenant, client_id, secret)
    return None

def resolve_subscription(projects, requested):
    """Map a vault key passed as --project to its subscription ID."""
    if requested and projects and requested in projects:
        subscription = projects[requested].get("subscription_id")
        if subscription:
            return str(subscription)
    return requested

def credential_for(subscription, load_projects, vault):
    """AZURE_* variables, then the vault's service principal, then az login.

    Exported variables are a deliberate per-shell choice, so they win. The
    vault entry is matched by subscription ID, as select-project.yml does.
    DefaultAzureCredential (az login, managed identity) comes last, since a
    cached login is easily stale or blocked by Entra ID security defaults.
    load_projects is only called when the environment did not decide.
    """
    credential = credential_from_environment()
    if credential:
        return credential, environment_source("AZURE_* environment", vault)
    key, entry = match_project(load_projects(), subscription, ("subscription_id",))
    if entry is not None:
        missing = [name for name in ("tenant_id", "client_id", "client_secret")
                   if not str(entry.get(name, "") or "").strip()]
        if missing:
            raise ValueError(f"vault entry '{key}' is missing {', '.join(missing)}")
        return ClientSecretCredential(
            str(entry["tenant_id"]), str(entry["client_id"]),
            str(entry["client_secret"])), f"vault entry '{key}'"
    return DefaultAzureCredential(), "DefaultAzureCredential"

def catalog_scope(migration_catalog):
    subscriptions = set()
    for entry in migration_catalog["migrations"]:
        if entry.get("target_platform") != "azure":
            continue
        subscription = entry.get("azure_subscription_id")
        if not isinstance(subscription, str) or not subscription.strip():
            continue
        subscriptions.add(subscription)
    return subscriptions

def resource_ids(entry):
    """The boot image, then the data snapshots (or, on older rows, images)."""
    data = entry.get("azure_data_snapshot_ids", entry.get("azure_data_image_ids", []))
    return [entry.get("azure_boot_image_id"), *data]

def find_missing_resources(client, subscription, migration_catalog):
    """Completed Azure rows whose managed images or snapshots no longer exist."""
    stale = []
    for entry in migration_catalog["migrations"]:
        if entry.get("target_platform") != "azure" or entry.get("status") != "completed":
            continue
        if entry.get("azure_subscription_id") != subscription:
            continue
        ids = resource_ids(entry)
        if not ids or any(not isinstance(value, str) or not value.strip() for value in ids):
            raise ValueError(
                f"Azure migration {entry.get('clone_name', '<unknown>')} "
                "has invalid Azure resource IDs")
        missing = []
        for resource_id in ids:
            match = RESOURCE_ID_PATTERN.match(resource_id)
            if not match:
                raise ValueError(
                    f"{resource_id} recorded by migration "
                    f"{entry.get('clone_name', '<unknown>')} is not a "
                    "managed image or snapshot resource ID")
            parts = parse_resource_id(resource_id)
            # The image ID names its own subscription. A row whose ID
            # disagrees with its azure_subscription_id column is malformed;
            # refuse rather than look it up with credentials for the wrong
            # subscription.
            if str(parts.get("subscription", "")).lower() != subscription.lower():
                raise ValueError(
                    f"{resource_id} is not in subscription {subscription}")
            collection = (client.images if match.group(1).lower() == "images"
                          else client.snapshots)
            try:
                collection.get(parts["resource_group"], parts["name"])
            except ResourceNotFoundError:
                missing.append(parts["name"])
        if missing:
            stale.append((entry, missing))
    return stale

def candidate_count(migration_catalog, subscription):
    return sum(
        1 for entry in migration_catalog["migrations"]
        if entry.get("target_platform") == "azure"
        and entry.get("azure_subscription_id") == subscription)

def main(argv=None):
    repo_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-catalog", type=Path,
                        default=repo_dir / "catalogs/migration-catalog.json")
    parser.add_argument("--subscription", "--project", dest="subscription",
                        help="only reconcile this Azure subscription ID "
                             "(or vault key)")
    parser.add_argument("--vault", type=Path, default=repo_dir / "vaults/azure.yml",
                        help="Azure vault for the service principal when AZURE_* is unset "
                             "(default: vaults/azure.yml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report stale entries without writing the catalog")
    parser.add_argument("--force", action="store_true",
                        help="allow removal of more than half of the scoped entries")
    args = parser.parse_args(argv)

    migration_catalog = read_snapshot(args.migration_catalog, "migrations")
    subscriptions = catalog_scope(migration_catalog)
    if not subscriptions:
        print("no Azure catalog entries found")
        return 0
    loaded = {}
    def load_projects():
        if "projects" not in loaded:
            loaded["projects"] = vault_projects(args.vault, "azure_projects")
        return loaded["projects"]
    # A --project that is not a catalog subscription may be a vault key.
    requested = args.subscription
    if requested and requested not in subscriptions:
        requested = resolve_subscription(load_projects(), requested)
    subscription = requested or (
        next(iter(subscriptions)) if len(subscriptions) == 1 else None)
    if not subscription:
        raise ValueError(
            "catalog contains multiple Azure subscriptions; provide --subscription")
    subscription = str(subscription)

    credential, source = credential_for(subscription, load_projects, args.vault)
    client = ComputeManagementClient(credential, subscription)
    print(f"Azure scope: subscription={subscription} (credentials: {source})")
    stale = find_missing_resources(client, subscription, migration_catalog)
    retired = [
        entry for entry in migration_catalog["migrations"]
        if entry.get("target_platform") == "azure"
        and entry.get("status") == "retired"
        and entry.get("azure_subscription_id") == subscription]

    for entry, missing in stale:
        print(
            f"removing migration {entry.get('vm_name', '<unknown>')} "
            f"{entry.get('clone_name', '<unknown>')}: missing resource(s) "
            f"{', '.join(missing)}")
    for entry in retired:
        print(
            f"removing previously retired migration "
            f"{entry.get('vm_name', '<unknown>')} {entry.get('clone_name', '<unknown>')}")

    stale_total = len(stale)
    total = stale_total + len(retired)
    if not total:
        print("no stale Azure catalog entries found")
        return 0
    if args.dry_run:
        print(f"{total} entries would be removed (--dry-run, catalog not written)")
        return 0
    scoped = candidate_count(migration_catalog, subscription)
    if stale_total and scoped and stale_total * 2 > scoped and not args.force:
        raise ValueError(
            f"refusing to remove {stale_total} of {scoped} scoped entries; "
            "inspect --dry-run and rerun with --force if this is intentional")
    changed = remove_migration_entries(
        args.migration_catalog, stale, retired,
        "azure", "clone_name", project_id=subscription)
    print(f"removed {changed} migration entries")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        AzureError,
    ) as error:
        raise SystemExit(f"reconcile-catalog-azure: {error}")
