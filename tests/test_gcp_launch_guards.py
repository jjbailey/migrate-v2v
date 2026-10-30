#!/usr/bin/env python3
# tests/test_gcp_launch_guards.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""Exercise the GCP launch guards without calling a cloud API."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DISK_TASKS = yaml.safe_load((ROOT / "playbooks/tasks/launch-gcp-data-disk.yml").read_text())
LAUNCH_TASKS = yaml.safe_load((ROOT / "playbooks/launch-gcp-instance.yml").read_text())[0]["tasks"]

def task_named(tasks, name):
    for task in tasks:
        if task.get("name") == name:
            return task
        if "block" in task:
            try:
                return task_named(task["block"], name)
            except LookupError:
                pass
    raise LookupError(name)

@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is required")
class GcpLaunchGuardTests(unittest.TestCase):
    def run_tasks(self, tasks):
        with tempfile.TemporaryDirectory() as directory:
            playbook = Path(directory) / "guards.yml"
            playbook.write_text(yaml.safe_dump([{
                "hosts": "localhost", "connection": "local", "gather_facts": False,
                "tasks": tasks,
            }], sort_keys=False))
            return subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(playbook)],
                text=True, capture_output=True, timeout=30,
            )

    def test_long_instance_name_keeps_distinct_disk_suffixes(self):
        result = self.run_tasks([
            {"ansible.builtin.set_fact": {
                "gcp_instance_name": "short-vm", "gcp_data_disk_index": 0,
            }},
            DISK_TASKS[0], DISK_TASKS[1],
            {"ansible.builtin.assert": {"that": [
                "gcp_data_disk_name == 'short-vm-data-1'",
            ]}},
            {"ansible.builtin.set_fact": {
                "gcp_instance_name": "a" * 63, "gcp_data_disk_index": 0,
            }},
            DISK_TASKS[0], DISK_TASKS[1],
            {"ansible.builtin.set_fact": {
                "first_disk": "{{ gcp_data_disk_name }}", "gcp_data_disk_index": 1,
            }},
            DISK_TASKS[0], DISK_TASKS[1],
            {"ansible.builtin.assert": {"that": [
                "first_disk != gcp_data_disk_name",
                "first_disk | length <= 63",
                "gcp_data_disk_name | length <= 63",
                "first_disk is match('.*-data-1$')",
                "gcp_data_disk_name is match('.*-data-2$')",
            ]}},
            {"ansible.builtin.set_fact": {
                "first_instance_disk": "{{ first_disk }}",
                "gcp_instance_name": "a" * 62 + "b",
                "gcp_data_disk_index": 0,
            }},
            DISK_TASKS[0], DISK_TASKS[1],
            {"ansible.builtin.assert": {"that": [
                "first_instance_disk != gcp_data_disk_name",
                "gcp_data_disk_name | length <= 63",
            ]}},
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_instance_must_match_migration(self):
        guard = task_named(LAUNCH_TASKS, "Refuse to reuse an instance from another migration")
        for recorded_migration, should_pass in (("migration-1", True), ("old-migration", False)):
            with self.subTest(recorded_migration=recorded_migration):
                result = self.run_tasks([{
                    "ansible.builtin.set_fact": {
                        "vm_name": "source-vm",
                        "gcp_instance_name": "target-vm",
                        "gcp_migration_id": "migration-1",
                        "gcp_instance_check": {
                            "rc": 0,
                            "stdout": json.dumps({"metadata": {"items": [
                                {"key": "MigratedFrom", "value": "source-vm"},
                                {"key": "SourceMigrationId", "value": recorded_migration},
                            ]}}),
                        },
                    },
                }, guard])
                self.assertEqual(result.returncode == 0, should_pass, result.stdout + result.stderr)

    def test_existing_disk_must_match_source_image(self):
        guard = task_named(DISK_TASKS, "Refuse a data disk made from another image")
        for recorded_project, recorded_image, should_pass in (
            ("p", "new-image", True),
            ("p", "old-image", False),
            ("other-project", "new-image", False),
        ):
            with self.subTest(recorded_project=recorded_project, recorded_image=recorded_image):
                result = self.run_tasks([{
                    "ansible.builtin.set_fact": {
                        "gcp_data_disk_name": "target-data-1",
                        "gcp_project": "p",
                        "gcp_data_image": "new-image",
                        "gcp_data_disk_check": {
                            "rc": 0,
                            "stdout": json.dumps({"sourceImage": f"projects/{recorded_project}/global/images/{recorded_image}"}),
                        },
                    },
                }, guard])
                self.assertEqual(result.returncode == 0, should_pass, result.stdout + result.stderr)

    def test_existing_device_must_hold_the_expected_disk(self):
        guard = task_named(DISK_TASKS, "Refuse a different disk at the expected device name")
        for disk_name, should_pass in (("target-data-1", True), ("other-disk", False)):
            with self.subTest(disk_name=disk_name):
                result = self.run_tasks([{
                    "ansible.builtin.set_fact": {
                        "gcp_instance_name": "target",
                        "gcp_data_disk_name": "target-data-1",
                        "gcp_data_device_name": "data-disk-1",
                        "gcp_attached_disks": {"stdout": json.dumps({"disks": [{
                            "deviceName": "data-disk-1",
                            "source": f"projects/p/zones/z/disks/{disk_name}",
                        }]})},
                    },
                }, guard])
                self.assertEqual(result.returncode == 0, should_pass, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
