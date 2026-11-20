#!/usr/bin/env python3
# bin/reconcile-catalog-gcp.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Remove GCP catalog entries when their resources are gone."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from catalog_common import (default_catalog_path, read_optional_snapshot, read_snapshot,
                            remove_deployment_entries, remove_migration_entries)
from vault_common import (default_vault_path, environment_source, match_project,
                          vault_projects)

import google.auth
import google.auth.exceptions
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

COMPUTE_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
COMPUTE_API = "https://compute.googleapis.com/compute/v1/projects"

def catalog_scope(migration_catalog, deployment_catalog):
    projects = set()
    for catalog, list_key in ((migration_catalog, "migrations"), (deployment_catalog, "deployments")):
        for entry in catalog[list_key]:
            if entry.get("target_platform") != "gcp":
                continue
            project = entry.get("gcp_project")
            if not isinstance(project, str) or not project.strip():
                continue
            projects.add(project)
    return projects

def default_project_id(projects, load_projects, credentials_file=None):
    """Infer one catalog project from configured GCP defaults, if possible."""
    if len(projects) == 1:
        return next(iter(projects))

    # Explicit Google project environment settings are the operator's chosen
    # default. Use those before the vault's single-project default.
    for name in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT"):
        project = os.environ.get(name, "").strip()
        if project:
            if project not in projects:
                raise ValueError(
                    f"default GCP project {project} from {name} has no catalog entries; "
                    f"catalog projects: {', '.join(sorted(projects)) or '<none>'}")
            return project

    # A single project in gcp.yml is also the playbooks' default selection.
    # When several vault entries exist, a unique overlap with the catalog is
    # still an unambiguous choice.
    if not credentials_file and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        configured = load_projects()
        vault_projects = set()
        for key, entry in (configured or {}).items():
            if not isinstance(entry, dict):
                continue
            project = str(
                entry.get("gcp_project") or entry.get("project_id") or key
            ).strip()
            if project:
                vault_projects.add(project)
        matches = vault_projects & projects
        if len(matches) == 1:
            return next(iter(matches))

    # google.auth.default() reports GOOGLE_CLOUD_PROJECT or the active gcloud
    # ADC project. Use it to scope a multi-project catalog to the user's
    # current GCP default; resources in all other projects stay untouched.
    if not credentials_file:
        try:
            credentials, project = google.auth.default(scopes=[COMPUTE_SCOPE])
        except google.auth.exceptions.GoogleAuthError:
            credentials, project = None, None
        project = (getattr(credentials, "project_id", None) or project or "")
        project = str(project).strip()
        if project:
            if project not in projects:
                raise ValueError(
                    f"default GCP project {project} from Google credentials has no catalog entries; "
                    f"catalog projects: {', '.join(sorted(projects)) or '<none>'}")
            return project

    return None

def validate_scope(migration_catalog, deployment_catalog, project_id):
    projects = catalog_scope(migration_catalog, deployment_catalog)
    if project_id not in projects:
        raise ValueError(
            f"catalog contains no GCP entries for project {project_id}")

def get_resource(session, url, cache):
    if url in cache:
        return cache[url]
    response = session.get(url, timeout=30)
    if response.status_code == 404:
        try:
            reasons = [item.get("reason") for item in
                       response.json().get("error", {}).get("errors", [])]
        except (AttributeError, TypeError, ValueError):
            reasons = []
        if "notFound" not in reasons:
            response.raise_for_status()
        resource = None
    else:
        response.raise_for_status()
        resource = response.json()
    cache[url] = resource
    return resource

def verify_project_and_zones(session, project_id, deployment_catalog):
    if get_resource(session, f"{COMPUTE_API}/{project_id}", {}) is None:
        raise ValueError(f"GCP project {project_id} does not exist or is inaccessible")
    zones = {entry.get("gcp_zone") for entry in deployment_catalog["deployments"]
             if entry.get("target_platform") == "gcp"
             and entry.get("gcp_project") == project_id
             and entry.get("status") != "retired"
             and entry.get("gcp_zone")}
    for zone in sorted(zones):
        if get_resource(session, f"{COMPUTE_API}/{project_id}/zones/{zone}", {}) is None:
            raise ValueError(
                f"GCP deployment catalog references missing zone {zone} in {project_id}; "
                "refusing to treat resources there as deleted")

def aggregated_resource_location(session, project_id, collection, name, cache):
    key = (collection, name)
    if key in cache:
        return cache[key]
    url = f"{COMPUTE_API}/{project_id}/aggregated/{collection}"
    page_token = None
    while True:
        params = {"filter": f"name = {name}", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        response = session.get(url, params=params, timeout=30)
        response.raise_for_status()
        body = response.json()
        for scope, scoped in (body.get("items") or {}).items():
            if any(resource.get("name") == name
                   for resource in ((scoped or {}).get(collection) or [])):
                cache[key] = scope
                return scope
        page_token = body.get("nextPageToken")
        if not page_token:
            cache[key] = None
            return None

def find_missing_resources(session, project_id, migration_catalog, deployment_catalog):
    image_cache, instance_cache, disk_cache = {}, {}, {}
    aggregate_cache = {}
    stale_migrations, stale_deployments = [], []

    for entry in migration_catalog["migrations"]:
        if entry.get("target_platform") != "gcp" or entry.get("status") != "completed":
            continue
        if entry.get("gcp_project") != project_id:
            continue
        image_names = [entry.get("gcp_boot_image_name"),
                       *(entry.get("gcp_data_image_names") or [])]
        if not image_names or any(not isinstance(name, str) or not name.strip() for name in image_names):
            raise ValueError(
                f"GCP migration {entry.get('clone_name', '<unknown>')} has invalid GCP image names")
        missing = []
        for image_name in image_names:
            url = f"{COMPUTE_API}/{project_id}/global/images/{image_name}"
            if get_resource(session, url, image_cache) is None:
                missing.append(image_name)
        if missing:
            stale_migrations.append((entry, missing))

    for entry in deployment_catalog["deployments"]:
        if entry.get("target_platform") != "gcp" or entry.get("status") == "retired":
            continue
        if entry.get("gcp_project") != project_id:
            continue
        instance_name, zone = entry.get(
            "gcp_instance_name"), entry.get("gcp_zone")
        if not instance_name or not zone:
            raise ValueError(
                f"GCP deployment for {entry.get('vm_name', '<unknown>')} has no gcp_instance_name or gcp_zone")
        disk_names = entry.get("gcp_data_disk_names", [])
        if not isinstance(disk_names, list):
            raise ValueError(
                f"GCP deployment {instance_name} has invalid gcp_data_disk_names")
        instance_url = f"{COMPUTE_API}/{project_id}/zones/{zone}/instances/{instance_name}"
        missing_instance = get_resource(
            session, instance_url, instance_cache) is None
        # A resource missing from the catalog's zone but present in another
        # zone is not proof of deletion: it may be a relaunch under the same
        # name (zone is part of the deployment identity, so both rows can be
        # legitimate) or a mis-recorded zone. Keep that row, say so, and go
        # on with the rest of the project rather than abort the whole run.
        elsewhere = None
        if missing_instance:
            location = aggregated_resource_location(
                session, project_id, "instances", instance_name, aggregate_cache)
            if location:
                elsewhere = f"instance {instance_name} is present in {location}"
        missing_disks = []
        for disk_name in disk_names if elsewhere is None else ():
            disk_url = f"{COMPUTE_API}/{project_id}/zones/{zone}/disks/{disk_name}"
            if get_resource(session, disk_url, disk_cache) is None:
                location = aggregated_resource_location(
                    session, project_id, "disks", disk_name, aggregate_cache)
                if location:
                    elsewhere = f"disk {disk_name} is present in {location}"
                    break
                missing_disks.append(disk_name)
        if elsewhere is not None:
            print(f"keeping deployment {entry.get('vm_name', '<unknown>')} {instance_name}: "
                  f"GCP {elsewhere}, not the catalog's zones/{zone}; "
                  "check that row by hand", file=sys.stderr)
            continue
        if missing_instance or missing_disks:
            stale_deployments.append((entry, missing_instance, missing_disks))
    return stale_migrations, stale_deployments

def candidate_count(migration_catalog, deployment_catalog, project_id):
    return sum(1 for entry in migration_catalog["migrations"] if entry.get("target_platform") == "gcp" and entry.get("status") == "completed" and entry.get("gcp_project") == project_id) + sum(1 for entry in deployment_catalog["deployments"] if entry.get("target_platform") == "gcp" and entry.get("status") != "retired" and entry.get("gcp_project") == project_id)

def credentials_for(project_id, load_projects, vault, credentials_file=None):
    """--credentials-file, then GOOGLE_APPLICATION_CREDENTIALS, then the
    vault, then the remaining Google defaults (gcloud login, metadata server).

    The vault entry is matched by key, gcp_project, or project_id, as
    select-project.yml does, and its service_account_key authenticates.
    load_projects is only called when neither the flag nor the environment
    decided.

    Returns (credentials, the project the credentials carry, source).
    """
    credential_args = {"scopes": [COMPUTE_SCOPE]}
    if credentials_file:
        credentials, credential_project = google.auth.load_credentials_from_file(
            credentials_file, **credential_args)
        credential_project = credential_project or getattr(credentials, "project_id", None)
        return credentials, credential_project, f"file {credentials_file}"
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        credentials, _ = google.auth.default(**credential_args)
        return (credentials, getattr(credentials, "project_id", None),
                environment_source("GOOGLE_APPLICATION_CREDENTIALS", vault))
    key, entry = match_project(load_projects(), project_id, ("gcp_project", "project_id"))
    if entry is not None:
        raw = entry.get("service_account_key", "")
        try:
            info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"vault entry '{key}' service_account_key is not valid JSON: {error}")
        credentials = service_account.Credentials.from_service_account_info(
            info, **credential_args)
        return credentials, info.get("project_id"), f"vault entry '{key}'"
    # google.auth.default() reports the ambient default project (gcloud
    # config, GOOGLE_CLOUD_PROJECT), which says nothing about where the
    # credentials live or what they can reach. Only a project the
    # credentials themselves carry, as a service account does, is a
    # reason to refuse.
    credentials, _ = google.auth.default(**credential_args)
    return (credentials, getattr(credentials, "project_id", None),
            "Google application default credentials")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-catalog", type=Path,
                        default=default_catalog_path("migration-catalog.json"))
    parser.add_argument("--deployment-catalog", type=Path,
                        default=default_catalog_path("deployment-catalog.json"))
    parser.add_argument(
        "--project-id",
        help="GCP project; inferred from a single catalog project, a single "
             "vault project, or the active Google default when unambiguous",
    )
    parser.add_argument("--credentials-file", type=Path,
                        help="service-account JSON, instead of the vault")
    parser.add_argument("--vault", type=Path, default=default_vault_path("gcp.yml"),
                        help="GCP vault for the service account when "
                             "GOOGLE_APPLICATION_CREDENTIALS is unset "
                             "(default: $MIGRATE_V2V_VAULT_DIR/gcp.yml, or "
                             "/work/migrate-v2v/vaults/gcp.yml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report stale entries without writing either catalog")
    parser.add_argument("--force", action="store_true",
                        help="allow removal of more than half of the scoped entries")
    args = parser.parse_args()

    migration_catalog = read_snapshot(args.migration_catalog, "migrations")
    deployment_catalog = read_optional_snapshot(args.deployment_catalog, "deployments")
    unscoped = [entry for catalog, list_key in (
        (migration_catalog, "migrations"), (deployment_catalog, "deployments"))
        for entry in catalog[list_key]
        if entry.get("target_platform") == "gcp"
        and entry.get("status") != "retired"
        and not str(entry.get("gcp_project") or "").strip()]
    for entry in unscoped:
        print(
            f"skipping {entry.get('vm_name', '<unknown>')} "
            f"{entry.get('clone_name') or entry.get('gcp_instance_name') or '<unknown>'}: "
            "no gcp_project recorded, so it cannot be checked or removed")
    projects = catalog_scope(migration_catalog, deployment_catalog)
    loaded = {}
    def load_projects():
        if "projects" not in loaded:
            loaded["projects"] = vault_projects(args.vault, "gcp_projects")
        return loaded["projects"]

    project_id = args.project_id or default_project_id(
        projects, load_projects, args.credentials_file)
    if not project_id:
        if not projects:
            raise ValueError("catalog contains no GCP projects to reconcile")
        raise ValueError(
            "catalog contains multiple GCP projects and no default project can "
            "be inferred; set GOOGLE_CLOUD_PROJECT, configure one matching "
            "project in gcp.yml, or provide --project-id")
    validate_scope(migration_catalog, deployment_catalog, project_id)
    credentials, credential_project, source = credentials_for(
        project_id, load_projects, args.vault, args.credentials_file)
    if credential_project and str(credential_project) != str(project_id):
        raise ValueError(
            f"resolved GCP credentials belong to project {credential_project}, "
            f"not --project-id {project_id}")
    session = AuthorizedSession(credentials)

    print(f"GCP scope: project_id={project_id} (credentials: {source})")
    verify_project_and_zones(session, project_id, deployment_catalog)
    stale_migrations, stale_deployments = find_missing_resources(
        session, project_id, migration_catalog, deployment_catalog)
    retired_migrations = [entry for entry in migration_catalog["migrations"] if entry.get("target_platform") == "gcp" and entry.get("status") == "retired" and entry.get("gcp_project") == project_id]
    retired_deployments = [entry for entry in deployment_catalog["deployments"] if entry.get("target_platform") == "gcp" and entry.get("status") == "retired" and entry.get("gcp_project") == project_id]

    for entry, missing in stale_migrations:
        print(
            f"removing migration {entry.get('vm_name', '<unknown>')} {entry.get('clone_name', '<unknown>')}: missing image(s) {', '.join(missing)}")
    for entry, missing_instance, missing_disks in stale_deployments:
        missing = (
            [f"instance {entry.get('gcp_instance_name', '<unknown>')}"] if missing_instance else [])
        if missing_disks:
            missing.append("disk(s) " + ", ".join(missing_disks))
        print(
            f"removing deployment {entry.get('vm_name', '<unknown>')} {entry.get('gcp_instance_name', '<unknown>')}: missing {'; '.join(missing)}")
    for entry in retired_migrations:
        print(
            f"removing previously retired migration {entry.get('vm_name', '<unknown>')} {entry.get('clone_name', '<unknown>')}")
    for entry in retired_deployments:
        print(
            f"removing previously retired deployment {entry.get('vm_name', '<unknown>')} {entry.get('gcp_instance_name', '<unknown>')}")

    stale_total = len(stale_migrations) + len(stale_deployments)
    total = stale_total + len(retired_migrations) + len(retired_deployments)
    if not total:
        print("no stale GCP catalog entries found")
        return 0
    if args.dry_run:
        print(f"{total} entries would be removed (--dry-run, catalogs not written)")
        return 0
    scope_total = candidate_count(migration_catalog, deployment_catalog, project_id)
    # Deliberate: a lone stale row always trips this guard and needs --force.
    if stale_total and scope_total and stale_total * 2 > scope_total and not args.force:
        raise ValueError(
            f"refusing to remove {stale_total} of {scope_total} scoped entries; inspect --dry-run and rerun with --force if this is intentional")
    migration_changed = remove_migration_entries(
        args.migration_catalog, stale_migrations, retired_migrations,
        "gcp", "clone_name", project_id=project_id)
    deployment_changed = remove_deployment_entries(
        args.deployment_catalog, stale_deployments, retired_deployments,
        "gcp", "gcp_instance_name", project_id=project_id)
    print(
        f"removed {migration_changed} migration and {deployment_changed} deployment entries")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        # Missing, unreadable or expired credentials (DefaultCredentialsError,
        # RefreshError): one line, not a traceback.
        google.auth.exceptions.GoogleAuthError,
    ) as error:
        raise SystemExit(f"reconcile-catalog-gcp: {error}")
