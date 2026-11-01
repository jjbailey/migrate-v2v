#!/usr/bin/env python3
# tests/test_azure_catalog_tools.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Azure catalog validation and reconcile, against fakes and temporary catalogs.

Nothing here reaches Azure: the reconcile tests hand find_missing_resources a
fake ComputeManagementClient, and main() runs with the credential loader and
client constructor patched out.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

def load_script(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

migration = load_script("append-migration-catalog.py", "append_migration_catalog_azure")
lookup_azure = load_script("lookup-migration-azure.py", "lookup_migration_azure")

try:
    reconcile_azure = load_script("reconcile-catalog-azure.py", "reconcile_azure")
except ImportError:  # the Azure SDK is not installed
    reconcile_azure = None

def azure_migration(vm, subscription="sub-a", boot=None, data=(), status="completed",
                    legacy_data_images=False):
    boot = boot or f"{vm}-migration-1-disk-0"
    def resource_id(name, collection):
        return (f"/subscriptions/{subscription}/resourceGroups/rg-a"
                f"/providers/Microsoft.Compute/{collection}/{name}")
    def image_id(name):
        return resource_id(name, "images")
    data_kind = "image" if legacy_data_images else "snapshot"
    return {
        "vm_name": vm, "source_vcenter": "vc.example",
        "source_datacenter": "dc-a", "clone_name": f"{vm}-azure-export-clone-1",
        "snapshot_name": "snap-1", "target_platform": "azure",
        "artifact_type": "vhd-disk-set",
        "artifact_manifest": {
            "manifest_version": 1, "disk_count": 1 + len(list(data)),
            "disks": [{"index": index, "boot_candidate": index == 0,
                       "capacity_bytes": 10}
                      for index in range(1 + len(list(data)))],
        },
        "status": status, "migrated_at": "2026-09-14T12:00:00Z",
        "azure_subscription_id": subscription,
        "azure_resource_group": "rg-a", "azure_location": "westus2",
        "azure_storage_account": "stgaccount1",
        "azure_storage_container": "migrate-v2v-images",
        "azure_blob_prefix": "migrate-v2v/1",
        "azure_hyper_v_generation": "V1",
        "azure_boot_image_name": boot,
        "azure_boot_image_id": image_id(boot),
        f"azure_data_{data_kind}_names": list(data),
        f"azure_data_{data_kind}_ids": [resource_id(name, data_kind + "s") for name in data],
    }

class AzureValidatorTests(unittest.TestCase):

    def test_valid_entry_passes(self):
        entry = azure_migration("vm-a", data=["vm-a-migration-1-disk-1"])
        migration.validate(entry)

    def test_image_order_is_enforced(self):
        entry = azure_migration("vm-a", data=["vm-a-migration-1-disk-1"])
        entry["azure_boot_image_name"] = "vm-a-migration-1-disk-1"
        with self.assertRaisesRegex(ValueError, "out of manifest disk order"):
            migration.validate(entry)

    def test_data_ids_must_match_data_names(self):
        entry = azure_migration("vm-a", data=["vm-a-migration-1-disk-1"])
        entry["azure_data_snapshot_ids"] = []
        with self.assertRaisesRegex(ValueError, "azure_data_snapshot_ids"):
            migration.validate(entry)

    def test_data_count_must_match_disks(self):
        entry = azure_migration("vm-a", data=["vm-a-migration-1-disk-1"])
        entry["artifact_manifest"]["disk_count"] = 3
        entry["artifact_manifest"]["disks"].append(
            {"index": 2, "boot_candidate": False, "capacity_bytes": 30})
        with self.assertRaisesRegex(ValueError, "disk count minus one"):
            migration.validate(entry)

    def test_image_ids_must_be_resource_ids(self):
        entry = azure_migration("vm-a")
        entry["azure_boot_image_id"] = "rg-a/vm-a-migration-1-disk-0"
        with self.assertRaisesRegex(ValueError, "managed image resource ID"):
            migration.validate(entry)

    def test_image_id_must_name_its_image(self):
        entry = azure_migration("vm-a")
        entry["azure_boot_image_id"] = (
            "/subscriptions/sub-a/resourceGroups/rg-a"
            "/providers/Microsoft.Compute/images/some-other-image")
        with self.assertRaisesRegex(ValueError, "does not name"):
            migration.validate(entry)

    def test_data_disks_must_be_snapshots(self):
        entry = azure_migration("vm-a", data=["vm-a-migration-1-disk-1"])
        entry["azure_data_snapshot_ids"] = [
            "/subscriptions/sub-a/resourceGroups/rg-a"
            "/providers/Microsoft.Compute/images/vm-a-migration-1-disk-1"]
        with self.assertRaisesRegex(ValueError, "managed snapshot resource ID"):
            migration.validate(entry)

    def test_rows_with_legacy_data_images_still_validate(self):
        entry = azure_migration("vm-a", data=["vm-a-migration-1-disk-1"],
                                legacy_data_images=True)
        migration.validate(entry)

    def test_hyper_v_generation_must_be_v1_or_v2(self):
        entry = azure_migration("vm-a")
        entry["azure_hyper_v_generation"] = "V3"
        with self.assertRaisesRegex(ValueError, "V1 or V2"):
            migration.validate(entry)

    def test_missing_fields_are_named(self):
        entry = azure_migration("vm-a")
        del entry["azure_location"]
        del entry["azure_storage_container"]
        with self.assertRaisesRegex(ValueError, "azure_location, azure_storage_container"):
            migration.validate(entry)

class AzureLookupTests(unittest.TestCase):

    def lookup(self, rows, *extra):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "migration-catalog.json"
            path.write_text(json.dumps({"schema_version": 1, "migrations": rows}))
            return subprocess.run(
                [sys.executable, str(ROOT / "bin" / "lookup-migration-azure.py"),
                 "--catalog", str(path), "--vm-name", "vm-a", *extra],
                capture_output=True, text=True, check=False)

    def test_projects_the_boot_image_and_data_snapshots(self):
        entry = azure_migration("vm-a", data=["vm-a-migration-1-disk-1"])
        projected = lookup_azure.project(entry)
        self.assertEqual(projected["azure_boot_image_name"], "vm-a-migration-1-disk-0")
        self.assertEqual(projected["azure_data_snapshot_names"], ["vm-a-migration-1-disk-1"])
        self.assertTrue(projected["azure_data_snapshot_ids"][0].endswith(
            "/snapshots/vm-a-migration-1-disk-1"))
        self.assertEqual(projected["migration_id"], "vm-a-azure-export-clone-1")

    def test_newest_row_for_the_subscription_wins(self):
        rows = [
            dict(azure_migration("vm-a"), migrated_at="2026-09-01T00:00:00Z",
                 clone_name="old"),
            dict(azure_migration("vm-a"), migrated_at="2026-09-02T00:00:00Z",
                 clone_name="new"),
            dict(azure_migration("vm-a", subscription="sub-b"),
                 migrated_at="2026-09-03T00:00:00Z", clone_name="other"),
        ]
        found = self.lookup(rows, "--project", "SUB-A")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(json.loads(found.stdout)["migration_id"], "new")

    def test_legacy_rows_with_data_images_are_refused(self):
        rows = [azure_migration("vm-a", data=["vm-a-migration-1-disk-1"],
                                legacy_data_images=True)]
        found = self.lookup(rows)
        self.assertNotEqual(found.returncode, 0)
        self.assertIn("migrate the VM again", found.stderr)
        self.assertNotIn("Traceback", found.stderr)

    def test_legacy_single_disk_rows_still_launch(self):
        rows = [azure_migration("vm-a", legacy_data_images=True)]
        found = self.lookup(rows)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(json.loads(found.stdout)["azure_data_snapshot_ids"], [])

class FakeImages:
    """images.get raises ResourceNotFoundError for the names in missing."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    def get(self, resource_group, name):
        self.calls.append((resource_group, name))
        if name in self.missing:
            raise load_resource_not_found()
        return {"name": name, "provisioning_state": "Succeeded"}

def load_resource_not_found():
    from azure.core.exceptions import ResourceNotFoundError
    return ResourceNotFoundError("not found")

class FakeClient:
    def __init__(self, missing=()):
        self.images = FakeImages(missing)
        self.snapshots = FakeImages(missing)

@unittest.skipIf(reconcile_azure is None, "needs the Azure SDK")
class AzureReconcileLogicTests(unittest.TestCase):

    def test_catalog_scope_collects_only_azure_subscriptions(self):
        migrations = {"migrations": [
            azure_migration("a"),
            {"target_platform": "gcp", "azure_subscription_id": "not-azure"},
            dict(azure_migration("b"), azure_subscription_id="  "),
            dict(azure_migration("c"), azure_subscription_id=None),
        ]}
        self.assertEqual(reconcile_azure.catalog_scope(migrations), {"sub-a"})

    def test_find_missing_resources_flags_only_missing_images(self):
        migrations = {"migrations": [
            azure_migration("a", data=["a-migration-1-disk-1"]),
            azure_migration("b"),
        ]}
        client = FakeClient(missing={"a-migration-1-disk-0"})
        stale = reconcile_azure.find_missing_resources(client, "sub-a", migrations)
        self.assertEqual([entry["vm_name"] for entry, _ in stale], ["a"])
        self.assertEqual(stale[0][1], ["a-migration-1-disk-0"])

    def test_find_missing_resources_checks_snapshots_and_legacy_images(self):
        migrations = {"migrations": [
            azure_migration("a", data=["a-migration-1-disk-1"]),
            azure_migration("b", data=["b-migration-1-disk-1"], legacy_data_images=True),
        ]}
        client = FakeClient(missing={"a-migration-1-disk-1", "b-migration-1-disk-1"})
        stale = reconcile_azure.find_missing_resources(client, "sub-a", migrations)
        self.assertEqual([(entry["vm_name"], missing) for entry, missing in stale],
                         [("a", ["a-migration-1-disk-1"]), ("b", ["b-migration-1-disk-1"])])
        self.assertIn(("rg-a", "a-migration-1-disk-1"), client.snapshots.calls)
        self.assertIn(("rg-a", "b-migration-1-disk-1"), client.images.calls)

    def test_find_missing_resources_refuses_a_foreign_subscription_id(self):
        entry = azure_migration("a")
        entry["azure_boot_image_id"] = (
            "/subscriptions/sub-b/resourceGroups/rg-a"
            "/providers/Microsoft.Compute/images/a-migration-1-disk-0")
        migrations = {"migrations": [entry]}
        with self.assertRaisesRegex(ValueError, "not in subscription sub-a"):
            reconcile_azure.find_missing_resources(FakeClient(), "sub-a", migrations)

    def test_find_missing_resources_refuses_a_malformed_id(self):
        entry = azure_migration("a")
        entry["azure_boot_image_id"] = "not-a-resource-id"
        migrations = {"migrations": [entry]}
        with self.assertRaisesRegex(ValueError, "managed image or snapshot resource ID"):
            reconcile_azure.find_missing_resources(FakeClient(), "sub-a", migrations)

    def test_find_missing_resources_skips_other_statuses_and_subscriptions(self):
        migrations = {"migrations": [
            azure_migration("failed", status="failed"),
            azure_migration("other", subscription="sub-b"),
        ]}
        client = FakeClient(missing={"failed-migration-1-disk-0",
                                      "other-migration-1-disk-0"})
        self.assertEqual(
            reconcile_azure.find_missing_resources(client, "sub-a", migrations), [])

@unittest.skipIf(reconcile_azure is None, "needs the Azure SDK")
class AzureReconcileMainTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        self.migrations = self.tmp / "migration-catalog.json"

    def write(self, migrations):
        self.migrations.write_text(
            json.dumps({"schema_version": 1, "migrations": migrations}))

    def names(self):
        return [entry["vm_name"]
                for entry in json.loads(self.migrations.read_text())["migrations"]]

    def run_main(self, *args, client=None):
        with mock.patch.object(reconcile_azure, "credential_from_environment",
                               return_value=None), \
             mock.patch.object(reconcile_azure, "ComputeManagementClient",
                               return_value=client or FakeClient()):
            return reconcile_azure.main(
                ["--migration-catalog", str(self.migrations), *args])

    def test_removes_stale_rows_and_keeps_the_rest(self):
        self.write([azure_migration("a"), azure_migration("b"),
                    azure_migration("c")])
        status = self.run_main(client=FakeClient(missing={"a-migration-1-disk-0"}))
        self.assertEqual(status, 0)
        self.assertEqual(self.names(), ["b", "c"])

    def test_dry_run_leaves_the_catalog_alone(self):
        self.write([azure_migration("a")])
        before = self.migrations.read_text()
        status = self.run_main("--dry-run",
                               client=FakeClient(missing={"a-migration-1-disk-0"}))
        self.assertEqual(status, 0)
        self.assertEqual(self.migrations.read_text(), before)

    def test_removes_previously_retired_rows(self):
        self.write([azure_migration("a", status="retired")])
        status = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(self.names(), [])

    def test_refuses_unusually_large_removals(self):
        self.write([azure_migration("a"), azure_migration("b")])
        before = self.migrations.read_text()
        client = FakeClient(missing={"a-migration-1-disk-0", "b-migration-1-disk-0"})
        with self.assertRaisesRegex(ValueError, "refusing to remove 2 of 2"):
            self.run_main(client=client)
        self.assertEqual(self.migrations.read_text(), before)
        status = self.run_main("--force", client=client)
        self.assertEqual(status, 0)
        self.assertEqual(self.names(), [])

    def test_needs_subscription_when_catalog_spans_subscriptions(self):
        self.write([azure_migration("a"), azure_migration("b", subscription="sub-b")])
        with self.assertRaisesRegex(ValueError, "multiple Azure subscriptions"):
            self.run_main()

    def test_no_azure_entries_is_a_clean_exit(self):
        self.write([{"target_platform": "gcp", "vm_name": "a"}])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.names(), ["a"])

if __name__ == "__main__":
    unittest.main()
