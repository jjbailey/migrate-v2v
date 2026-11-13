#!/usr/bin/env python3
# bin/reconcile-catalog-azure.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Remove Azure catalog entries when their managed images, snapshots or VMs are gone.

A lone stale row (stale * 2 > scoped) is refused without --force by design.
"""

import argparse
import json
import os
import re
from pathlib import Path

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.core.tools import parse_resource_id

from catalog_common import (default_catalog_path, read_snapshot,
                            remove_deployment_entries, remove_migration_entries)
from vault_common import (default_vault_path, environment_source, match_project,
                          vault_projects)

# A managed image or snapshot resource ID, as azure_rm_image_info and
# azure_rm_snapshot_info report them.
RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+"
    r"/providers/Microsoft[.]Compute/(images|snapshots)/[^/]+$",
    re.IGNORECASE,
)

# ARM error codes that mean the resource itself is gone. A foreign tenant or
# subscription yields SubscriptionNotFound, which azure-core maps to the same
# ResourceNotFoundError and must not be read as "deleted".
NOT_FOUND_CODES = {"ResourceNotFound", "ResourceGroupNotFound"}

def is_gone(error):
    code = getattr(getattr(error, "error", None), "code", None)
    return code in NOT_FOUND_CODES

def verify_subscription_access(client, subscription):
    """List one image so a wrong tenant or subscription fails before the scan."""
    try:
        next(iter(client.images.list()), None)
    except AzureError as error:
        raise ValueError(
            f"cannot access Azure subscription {subscription} with these "
            f"credentials: {error}") from error

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

def catalog_scope(migration_catalog, deployment_catalog=None):
    subscriptions = set()
    catalogs = [(migration_catalog, "migrations")]
    if deployment_catalog is not None:
        catalogs.append((deployment_catalog, "deployments"))
    for catalog, list_key in catalogs:
        for entry in catalog[list_key]:
            if entry.get("target_platform") != "azure":
                continue
            subscription = entry.get("azure_subscription_id")
            if not isinstance(subscription, str) or not subscription.strip():
                continue
            subscriptions.add(subscription)
    return subscriptions

def same_subscription(entry, subscription):
    """Subscription IDs are GUIDs, which Azure compares case-insensitively."""
    return str(entry.get("azure_subscription_id", "")).casefold() == str(subscription).casefold()

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
        if not same_subscription(entry, subscription):
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
            except ResourceNotFoundError as error:
                if not is_gone(error):
                    raise
                missing.append(parts["name"])
        if missing:
            stale.append((entry, missing))
    return stale

def find_missing_deployments(client, subscription, deployment_catalog):
    """Live Azure deployment rows whose VM no longer exists."""
    stale = []
    for entry in deployment_catalog["deployments"]:
        if entry.get("target_platform") != "azure" or entry.get("status") == "retired":
            continue
        if not same_subscription(entry, subscription):
            continue
        vm_name, group = entry.get("azure_vm_name"), entry.get("azure_resource_group")
        if not vm_name or not group:
            raise ValueError(
                f"Azure deployment for {entry.get('vm_name', '<unknown>')} "
                "has no azure_vm_name or azure_resource_group")
        try:
            client.virtual_machines.get(group, vm_name)
        except ResourceNotFoundError as error:
            if not is_gone(error):
                raise
            stale.append((entry, vm_name))
    return stale

def candidate_count(migration_catalog, subscription, deployment_catalog=None):
    migrations = sum(
        1 for entry in migration_catalog["migrations"]
        if entry.get("target_platform") == "azure"
        and entry.get("status") == "completed"
        and same_subscription(entry, subscription))
    deployments = sum(
        1 for entry in (deployment_catalog or {"deployments": []})["deployments"]
        if entry.get("target_platform") == "azure"
        and entry.get("status") != "retired"
        and same_subscription(entry, subscription))
    return migrations + deployments

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-catalog", type=Path,
                        default=default_catalog_path("migration-catalog.json"))
    parser.add_argument("--deployment-catalog", type=Path,
                        default=default_catalog_path("deployment-catalog.json"))
    parser.add_argument("--subscription", "--project", dest="subscription",
                        help="only reconcile this Azure subscription ID "
                             "(or vault key)")
    parser.add_argument("--vault", type=Path, default=default_vault_path("azure.yml"),
                        help="Azure vault for the service principal when AZURE_* is unset "
                             "(default: $MIGRATE_V2V_VAULT_DIR/azure.yml, or "
                             "/work/migrate-v2v/vaults/azure.yml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report stale entries without writing either catalog")
    parser.add_argument("--force", action="store_true",
                        help="allow removal of more than half of the scoped entries")
    args = parser.parse_args(argv)

    migration_catalog = read_snapshot(args.migration_catalog, "migrations")
    # A deployment catalog that was never written is simply empty.
    if args.deployment_catalog.exists():
        deployment_catalog = read_snapshot(args.deployment_catalog, "deployments")
    else:
        deployment_catalog = {"schema_version": 1, "deployments": []}
    subscriptions = catalog_scope(migration_catalog, deployment_catalog)
    if not subscriptions:
        print("no Azure catalog entries found")
        return 0
    loaded = {}
    def load_projects():
        if "projects" not in loaded:
            loaded["projects"] = vault_projects(args.vault, "azure_projects")
        return loaded["projects"]
    # A --project that is not a catalog subscription may be a vault key.
    # Subscription IDs compare case-insensitively, as lookup-migration-azure
    # does; one spelled differently in the catalog, the vault, or on the
    # command line is the same subscription.
    folded = {item.casefold() for item in subscriptions}
    requested = args.subscription
    if requested and requested.casefold() not in folded:
        requested = resolve_subscription(load_projects(), requested)
    subscription = requested or (
        next(iter(subscriptions)) if len(folded) == 1 else None)
    if not subscription:
        raise ValueError(
            "catalog contains multiple Azure subscriptions; provide --subscription")
    subscription = str(subscription)
    if subscription.casefold() not in folded:
        raise ValueError(
            f"catalog contains no Azure entries for subscription {subscription}; "
            f"found: {', '.join(sorted(subscriptions))}")

    credential, source = credential_for(subscription, load_projects, args.vault)
    client = ComputeManagementClient(credential, subscription)
    print(f"Azure scope: subscription={subscription} (credentials: {source})")
    verify_subscription_access(client, subscription)
    stale = find_missing_resources(client, subscription, migration_catalog)
    stale_deployments = find_missing_deployments(
        client, subscription, deployment_catalog)
    retired = [
        entry for entry in migration_catalog["migrations"]
        if entry.get("target_platform") == "azure"
        and entry.get("status") == "retired"
        and same_subscription(entry, subscription)]
    retired_deployments = [
        entry for entry in deployment_catalog["deployments"]
        if entry.get("target_platform") == "azure"
        and entry.get("status") == "retired"
        and same_subscription(entry, subscription)]

    for entry, missing in stale:
        print(
            f"removing migration {entry.get('vm_name', '<unknown>')} "
            f"{entry.get('clone_name', '<unknown>')}: missing resource(s) "
            f"{', '.join(missing)}")
    for entry, vm_name in stale_deployments:
        print(
            f"removing deployment {entry.get('vm_name', '<unknown>')} "
            f"{vm_name}: missing Azure VM")
    for entry in retired:
        print(
            f"removing previously retired migration "
            f"{entry.get('vm_name', '<unknown>')} {entry.get('clone_name', '<unknown>')}")
    for entry in retired_deployments:
        print(
            f"removing previously retired deployment "
            f"{entry.get('vm_name', '<unknown>')} {entry.get('azure_vm_name', '<unknown>')}")

    stale_total = len(stale) + len(stale_deployments)
    total = stale_total + len(retired) + len(retired_deployments)
    if not total:
        print("no stale Azure catalog entries found")
        return 0
    if args.dry_run:
        print(f"{total} entries would be removed (--dry-run, catalogs not written)")
        return 0
    scoped = candidate_count(migration_catalog, subscription, deployment_catalog)
    if stale_total and scoped and stale_total * 2 > scoped and not args.force:
        raise ValueError(
            f"refusing to remove {stale_total} of {scoped} scoped entries; "
            "inspect --dry-run and rerun with --force if this is intentional")
    migration_changed = remove_migration_entries(
        args.migration_catalog, stale, retired,
        "azure", "clone_name", project_id=subscription)
    deployment_changed = remove_deployment_entries(
        args.deployment_catalog, stale_deployments, retired_deployments,
        "azure", "azure_vm_name", project_id=subscription)
    print(
        f"removed {migration_changed} migration and "
        f"{deployment_changed} deployment entries")
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
