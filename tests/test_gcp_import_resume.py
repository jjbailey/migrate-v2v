#!/usr/bin/env python3
# tests/test_gcp_import_resume.py
# vim: set tabstop=4 shiftwidth=4 expandtab:

"""When a resumed GCP migration reuses an image an earlier attempt left.

The real playbooks/tasks/migration/gcp/import.yml runs under ansible-playbook
with a stub gcloud first on PATH. The stub answers from a JSON state file
(the image's status and its import job's state) and logs every call, so
each case can check which steps ran without reaching GCP.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
IMPORT_TASKS = ROOT / "playbooks/tasks/migration/gcp/import.yml"
IMAGE = "vm-migration-123-disk-1"

GCLOUD_STUB = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["GCLOUD_STUB_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ["GCLOUD_STUB_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")

def absent():
    sys.stderr.write("ERROR: resource not found\n")
    sys.exit(1)

def save():
    json.dump(state, open(state_path, "w"))

fmt = next((a for a in args if a.startswith("--format=")), "")
if args[:3] == ["compute", "images", "describe"]:
    if state.get("image") is None:
        absent()
    if "selfLink" in fmt:
        print("https://compute/projects/p/global/images/" + args[3])
    else:
        print(state["image"])
elif args[:3] == ["compute", "images", "delete"]:
    state["image"] = None
    save()
elif args[:4] == ["compute", "migration", "image-imports", "describe"]:
    if state.get("import") is None:
        absent()
    if fmt.startswith("--format=json"):
        print("{}")
    elif "state" in fmt:
        print(state["import"])
    else:
        print("projects/p/locations/l/imageImports/" + args[4])
elif args[:4] == ["compute", "migration", "image-imports", "delete"]:
    state["import"] = None
    save()
elif args[:4] == ["compute", "migration", "image-imports", "create"]:
    # A fresh import that succeeds.
    state["import"] = "SUCCEEDED"
    state["image"] = "READY"
    save()
elif args[:2] == ["storage", "ls"]:
    sys.stderr.write("ERROR: One or more URLs matched no objects.\n")
    sys.exit(1)
elif args[:2] == ["storage", "cp"]:
    pass
else:
    sys.stderr.write("unexpected gcloud call\n")
    sys.exit(3)
'''

@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is required")
class GcpImportResumeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        stub_bin = self.tmp / "bin"
        stub_bin.mkdir()
        stub = stub_bin / "gcloud"
        stub.write_text(GCLOUD_STUB)
        stub.chmod(0o755)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        (self.run_dir / "disk-1.vmdk").write_text("")
        self.state_file = self.tmp / "state.json"
        self.log_file = self.tmp / "gcloud.log"
        self.env = {
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "GCLOUD_STUB_STATE": str(self.state_file),
            "GCLOUD_STUB_LOG": str(self.log_file),
            "ANSIBLE_LOCALHOST_WARNING": "false",
        }

    def run_import(self, image, import_state, marker=False, resume=True):
        self.state_file.write_text(json.dumps({"image": image, "import": import_state}))
        if marker:
            (self.run_dir / f"{IMAGE}.imported").write_text("SUCCEEDED\n")
        playbook = self.tmp / "play.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {
                "service_account_key": "{}",
                "artifact_manifest": {"disks": [{"index": 1, "path": "disk-1.vmdk"}]},
                "gcs_prefix": "migrate-v2v/123",
                "gcs_bucket": "bucket",
                "gcp_image_prefix": "vm-migration-123",
                "gcp_project": "p",
                "gcp_import_location": "l",
                "migration_run_dir": str(self.run_dir),
                "migration_resume_mode": resume,
                "gcp_replace_existing_image": False,
                "gcp_created_image_names": [],
                "gcp_gcs_object_names": [],
                "gcp_image_names": [],
                "gcp_image_selflinks": [],
                "gcp_reused_image_names": [],
                "gcp_absent_object_pattern": "not found|matched no objects",
                "gcp_absent_resource_pattern": "not found|does not exist",
                "gcp_import_poll_retries": 2,
                "gcp_import_poll_delay": 0,
                "gcp_delete_poll_retries": 2,
                "gcp_delete_poll_delay": 0,
                "gcp_image_lookup_retries": 2,
                "gcp_image_lookup_delay": 0,
            },
            "tasks": [
                {"ansible.builtin.include_tasks": str(IMPORT_TASKS)},
                {"ansible.builtin.copy": {
                    "dest": str(self.tmp / "result.json"),
                    "content": "{{ {'reused': gcp_reused_image_names,"
                               " 'images': gcp_image_names} | to_json }}",
                }},
            ],
        }], sort_keys=False))
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(playbook)],
            env=self.env, text=True, capture_output=True, timeout=120,
        )
        calls = self.log_file.read_text().splitlines() if self.log_file.exists() else []
        outcome = None
        if (self.tmp / "result.json").exists():
            outcome = json.loads((self.tmp / "result.json").read_text())
        return result, calls, outcome

    @staticmethod
    def called(calls, *words):
        prefix = " ".join(words)
        return any(call.startswith(prefix) for call in calls)

    def assert_reimported(self, result, calls, outcome):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outcome["reused"], [])
        self.assertTrue(self.called(calls, "compute images delete", IMAGE), calls)
        self.assertTrue(self.called(calls, "storage cp"), calls)
        self.assertTrue(self.called(calls, "compute migration image-imports create", IMAGE), calls)
        self.assertEqual(outcome["images"], [IMAGE])
        self.assertTrue((self.run_dir / f"{IMAGE}.imported").exists())

    def test_ready_image_with_succeeded_import_is_reused(self):
        result, calls, outcome = self.run_import("READY", "SUCCEEDED")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outcome["reused"], [IMAGE])
        self.assertEqual(outcome["images"], [IMAGE])
        self.assertFalse(self.called(calls, "storage cp"), calls)
        self.assertFalse(self.called(calls, "compute migration image-imports create"), calls)
        self.assertFalse(self.called(calls, "compute images delete"), calls)
        self.assertTrue(self.called(calls, "compute migration image-imports delete", IMAGE), calls)

    def test_ready_image_with_marker_and_no_import_resource_is_reused(self):
        result, calls, outcome = self.run_import("READY", None, marker=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(outcome["reused"], [IMAGE])
        self.assertFalse(self.called(calls, "compute migration image-imports create"), calls)

    def test_image_from_a_failed_import_is_replaced(self):
        self.assert_reimported(*self.run_import("READY", "FAILED"))

    def test_failed_image_is_replaced(self):
        self.assert_reimported(*self.run_import("FAILED", "SUCCEEDED"))

    def test_ready_image_without_marker_or_import_resource_is_replaced(self):
        self.assert_reimported(*self.run_import("READY", None))

    def test_import_still_running_is_not_reused(self):
        # The stub never finishes the earlier import, so the resume waits
        # out its poll window and fails rather than catalog an unknown image.
        result, calls, outcome = self.run_import("PENDING", "RUNNING")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(outcome)
        self.assertIn("still", result.stdout)
        self.assertFalse(self.called(calls, "compute images delete"), calls)
        self.assertFalse(self.called(calls, "compute migration image-imports delete"), calls)

    def test_fresh_run_refuses_to_overwrite_without_replacement(self):
        result, calls, outcome = self.run_import("READY", None, resume=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stdout)
        self.assertFalse(self.called(calls, "compute images delete"), calls)

if __name__ == "__main__":
    unittest.main()
