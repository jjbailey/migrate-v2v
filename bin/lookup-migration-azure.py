#!/usr/bin/env python3
# bin/lookup-migration-azure.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Find the newest completed Azure migration for a VM."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lookup_migration_common import main  # noqa: E402

def normalize_location(value):
    return str(value or "").lower().replace(" ", "")

def project_filter(args):
    wanted_project = None if args.project is None else str(args.project).strip().lower()
    wanted_region = normalize_location(args.region) if args.region else None
    if wanted_project is None and wanted_region is None:
        return None

    def matches(entry):
        if (wanted_project is not None
                and str(entry.get("azure_subscription_id", "")).lower() != wanted_project):
            return False
        # "West US 2" and "westus2" name the same location.
        return (wanted_region is None
                or normalize_location(entry.get("azure_location")) == wanted_region)
    return matches

def project(entry):
    # Azure cannot create a managed disk from a managed image, so a row from
    # before data disks were stored as snapshots cannot have its data disks
    # attached. A single-disk row of either kind launches fine.
    if entry.get("azure_data_image_ids") and "azure_data_snapshot_ids" not in entry:
        raise SystemExit(
            f"lookup-migration-azure: migration {entry.get('clone_name', '<unknown>')} "
            "recorded its data disks as managed images, which Azure cannot "
            "turn into disks; migrate the VM again to store them as snapshots")
    return {
        "vm_name": entry["vm_name"],
        "azure_subscription_id": entry["azure_subscription_id"],
        "azure_resource_group": entry["azure_resource_group"],
        "azure_location": entry["azure_location"],
        "azure_boot_image_name": entry["azure_boot_image_name"],
        "azure_boot_image_id": entry["azure_boot_image_id"],
        "azure_data_snapshot_names": entry.get("azure_data_snapshot_names", []),
        "azure_data_snapshot_ids": entry.get("azure_data_snapshot_ids", []),
        "azure_hyper_v_generation": entry.get("azure_hyper_v_generation", ""),
        "migration_id": entry.get("clone_name", ""),
    }

if __name__ == "__main__":
    main(
        "lookup-migration-azure",
        "azure",
        project,
        parser_options={"with_project": True, "with_region": True},
        entry_filter=project_filter,
    )
