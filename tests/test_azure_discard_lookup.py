#!/usr/bin/env python3
# tests/test_azure_discard_lookup.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Exercise the Azure datastore-direct discard's blob lookup without Azure."""

import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DISCARD_TASKS = yaml.safe_load(
    (ROOT / "playbooks/tasks/migration/azure/discard-vddk.yml").read_text())[0]["block"]
BLOB_MODULE = (Path.home() / ".ansible/collections/ansible_collections/azure/azcollection"
               / "plugins/modules/azure_rm_storageblob_info.py")

def task_named(name):
    for task in DISCARD_TASKS:
        if task.get("name") == name:
            return copy.deepcopy(task)
    raise LookupError(name)

LIST_TASK = task_named("List the run's staged blobs")
QUEUE_TASK = task_named("Queue the retained run's images, snapshots, and blobs for removal")

def listing_failing_with(message):
    """The blob listing task, with the module swapped for one that fails."""
    task = copy.deepcopy(LIST_TASK)
    del task["azure.azcollection.azure_rm_storageblob_info"]
    task["ansible.builtin.fail"] = {"msg": message}
    return task

STUB_FACTS = {"ansible.builtin.set_fact": {
    "azure_image_prefix": "vm-azure-vddk-1",
    "azure_discard_images": {"images": []},
    "azure_discard_snapshots": {"state": []},
    "azure_discard_storage_account": {"storageaccounts": [{"name": "account"}]},
}}

@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is required")
class AzureDiscardLookupTests(unittest.TestCase):
    def run_tasks(self, tasks):
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / "discard.yml"
            playbook.write_text(yaml.safe_dump([{
                "hosts": "localhost", "connection": "local", "gather_facts": False,
                "tasks": tasks,
            }], sort_keys=False))
            return subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(playbook)],
                text=True, capture_output=True, timeout=60,
            )

    def test_listed_blobs_are_queued_for_removal(self):
        # azure_rm_storageblob_info returns its listing under "blob".
        result = self.run_tasks([
            STUB_FACTS,
            {"ansible.builtin.set_fact": {"azure_discard_blobs": {"blob": [
                {"name": "vm-azure-vddk-1/disk-0.vhd"},
                {"name": "vm-azure-vddk-1/disk-1.vhd"},
            ]}}},
            QUEUE_TASK,
            {"ansible.builtin.assert": {"that": [
                "azure_blob_names == ['vm-azure-vddk-1/disk-0.vhd', 'vm-azure-vddk-1/disk-1.vhd']",
            ]}},
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_absent_container_is_an_empty_listing(self):
        result = self.run_tasks([
            STUB_FACTS,
            listing_failing_with(
                "Error list container blob staging - The specified container does not"
                " exist.\nRequestId:0\nErrorCode:ContainerNotFound"),
            QUEUE_TASK,
            {"ansible.builtin.assert": {"that": ["azure_blob_names == []"]}},
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_other_listing_failures_fail_the_lookup(self):
        result = self.run_tasks([
            STUB_FACTS,
            listing_failing_with(
                "Error list container blob staging - This request is not authorized"
                " to perform this operation.\nErrorCode:AuthorizationFailure"),
            {"name": "Marker after the lookup", "ansible.builtin.set_fact": {"marker": True}},
        ])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AuthorizationFailure", result.stdout)
        self.assertNotIn("Marker after the lookup", result.stdout)

@unittest.skipUnless(BLOB_MODULE.is_file(), "azure.azcollection is not installed")
class AzureBlobModuleContractTests(unittest.TestCase):
    def test_module_returns_the_listing_under_blob(self):
        # discard-vddk.yml reads azure_discard_blobs.blob; a collection
        # upgrade that renames the key would silently empty every discard.
        self.assertIn("self.results['blob'] = response", BLOB_MODULE.read_text())

if __name__ == "__main__":
    unittest.main()
